"""skillstem hot-loads SKILL.md directories and keeps the brainstem wire contract."""
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import skillstem  # noqa: E402


def write_skill(root: Path, name: str, description: str, body: str = "Do the thing.", files: dict | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    for relative, content in (files or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


def test_frontmatter_parsing_handles_quotes_and_missing_frontmatter():
    meta, body = skillstem.parse_skill_md('---\nname: "demo"\ndescription: \'A demo\'\n---\n\n# Demo\n')
    assert meta == {"name": "demo", "description": "A demo"} and body == "# Demo"
    assert skillstem.parse_skill_md("just text") == ({}, "just text")


def test_index_hot_loads_new_changed_and_deleted_skills(tmp_path):
    index = skillstem.SkillIndex(tmp_path)
    assert index.refresh() == {}
    write_skill(tmp_path, "alpha", "First skill")
    skills = index.refresh()
    assert list(skills) == ["alpha"] and skills["alpha"].description == "First skill"
    time.sleep(0.01)
    write_skill(tmp_path, "alpha", "First skill, revised", body="New body.")
    os.utime(tmp_path / "alpha" / "SKILL.md", ns=(time.time_ns(), time.time_ns()))
    assert index.refresh()["alpha"].body == "New body."
    write_skill(tmp_path, "beta", "Second skill")
    assert list(index.refresh()) == ["alpha", "beta"]
    (tmp_path / "alpha" / "SKILL.md").unlink()
    assert list(index.refresh()) == ["beta"]
    assert "beta: Second skill" in index.index_text()


def test_skill_files_are_confined_to_the_skill_directory(tmp_path):
    write_skill(tmp_path, "alpha", "x", files={"references/notes.md": "hello", "scripts/run.sh": "echo hi"})
    (tmp_path / "secret.txt").write_text("nope")
    skill = skillstem.SkillIndex(tmp_path).refresh()["alpha"]
    assert skill.files() == ["references/notes.md", "scripts/run.sh"]
    assert skillstem.safe_skill_file(skill, "references/notes.md").read_text() == "hello"
    with pytest.raises(FileNotFoundError):
        skillstem.safe_skill_file(skill, "../secret.txt")


class _FakeSession:
    def __init__(self, kwargs, script):
        self.kwargs, self.script, self.session_id = kwargs, script, "sdk-session-1"
        self._handlers = []

    def on(self, handler):
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler)

    async def send_and_wait(self, prompt, *, timeout):
        self.script.append(("prompt", prompt))
        from copilot import ToolInvocation
        for tool in self.kwargs["tools"]:
            if tool.name == "load_skill":
                out = await tool.handler(ToolInvocation(arguments={"name": "alpha"}))
                self.script.append(("load_skill", json.loads(out.text_result_for_llm)))
            if tool.name == "read_skill_file":
                out = await tool.handler(ToolInvocation(arguments={"name": "alpha", "path": "references/notes.md"}))
                self.script.append(("read_skill_file", json.loads(out.text_result_for_llm)))
                out = await tool.handler(ToolInvocation(arguments={"name": "alpha", "path": "../escape"}))
                self.script.append(("read_skill_file_escape", json.loads(out.text_result_for_llm)))
        for handler in self._handlers:
            handler(SimpleNamespace(type=SimpleNamespace(value="assistant.message"), data=SimpleNamespace(content="Used alpha.")))

    async def disconnect(self):
        self.script.append(("disconnect", None))


class _FakeClient:
    def __init__(self):
        self.script, self.session_kwargs = [], None

    async def start(self):
        self.script.append(("start", None))

    async def create_session(self, **kwargs):
        self.session_kwargs = kwargs
        return _FakeSession(kwargs, self.script)


def _runtime(tmp_path, client):
    settings = skillstem.load_settings({"SKILLS_PATH": tmp_path / "skills", "SOUL_PATH": tmp_path / "SOUL.md", "COPILOT_USE_LOGGED_IN_USER": True})
    return skillstem.SkillstemRuntime(settings, client_factory=lambda: client)


def test_chat_keeps_the_brainstem_contract_and_loads_skills_through_tools(tmp_path):
    (tmp_path / "SOUL.md").write_text("You are the test soul.")
    write_skill(tmp_path / "skills", "alpha", "Answers alpha questions", body="Always say ALPHA.", files={"references/notes.md": "alpha notes"})
    client = _FakeClient()
    runtime = _runtime(tmp_path, client)
    result = runtime.chat("hi", [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}], "sess-9")
    assert result["response"] == "Used alpha."
    assert result["session_id"] == "sess-9"
    assert result["skills_used"] == ["alpha"]
    assert [entry["tool"] for entry in result["agent_logs"] if "tool" in entry] == ["load_skill", "read_skill_file"]
    system = client.session_kwargs["system_message"]["content"]
    assert system.startswith("You are the test soul.") and "- alpha: Answers alpha questions" in system
    assert [t.name for t in client.session_kwargs["tools"]] == ["load_skill", "read_skill_file"]
    assert client.session_kwargs["skill_directories"] == [str(tmp_path / "skills")]
    steps = dict((k, v) for k, v in client.script if v is not None)
    assert steps["prompt"].startswith("Earlier in this conversation:") and steps["prompt"].endswith("User: hi")
    assert steps["load_skill"]["instructions"] == "Always say ALPHA." and steps["load_skill"]["files"] == ["references/notes.md"]
    assert steps["read_skill_file"]["content"] == "alpha notes"
    assert "error" in steps["read_skill_file_escape"]
    assert client.script[-1] == ("disconnect", None)


def test_http_surface_matches_the_brainstem(tmp_path):
    write_skill(tmp_path / "skills", "alpha", "Answers alpha questions")
    client = _FakeClient()
    runtime = _runtime(tmp_path, client)
    app = skillstem.create_app(runtime.settings, runtime)
    http = app.test_client()
    health = http.get("/health").get_json()
    assert health["engine"] == "skillstem" and health["skills"] == ["alpha"] and health["auth"] == "logged-in-user"
    assert http.get("/skills").get_json()["skills"][0]["name"] == "alpha"
    assert http.post("/chat", json={"user_input": "   "}).status_code == 400
    reply = http.post("/chat", json={"user_input": "hello", "conversation_history": [], "session_id": "s1"}).get_json()
    assert reply["response"] == "Used alpha." and reply["session_id"] == "s1" and reply["skills_used"] == ["alpha"]


def test_input_and_history_are_bounded(tmp_path):
    runtime = _runtime(tmp_path, _FakeClient())
    with pytest.raises(ValueError):
        runtime.chat("x" * (runtime.settings["MAX_INPUT_CHARS"] + 1))
    client = _FakeClient()
    runtime = _runtime(tmp_path, client)
    long_history = [{"role": "user", "content": "y" * 3000} for _ in range(40)]
    runtime.chat("hi", long_history)
    prompt = client.script[1][1]
    assert prompt.count("User: yyy") <= skillstem.MAX_HISTORY_TURNS
