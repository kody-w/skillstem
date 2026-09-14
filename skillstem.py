#!/usr/bin/env python3
"""skillstem: a brainstem-compatible harness on the GitHub Copilot SDK that hot-loads SKILL.md.

Wire contract (mirrors the RAPP brainstem so existing clients keep working):

    POST /chat   {"user_input": str, "conversation_history": [{"role","content"}], "session_id": str}
              -> {"response": str, "session_id": str, "model": str|null, "agent_logs": [...], "skills_used": [...]}
    GET  /health -> {"status": "ok", "engine": "skillstem", "skills": [...], "soul": path, "model": ...}
    GET  /skills -> the live skill index

Hot-loading loop (the point of this harness): every request scans SKILLS_PATH for
``*/SKILL.md`` (Agent Skills layout: one directory per skill, YAML frontmatter with
``name`` and ``description``, optional ``scripts/``, ``references/``, ``assets/``).
Changed or new files are re-read by mtime; deleted skills disappear. Nothing is
imported or executed. The system message carries SOUL.md plus a one-line index
of every skill (progressive disclosure); the model calls ``load_skill(name)`` to
pull a skill's full body and ``read_skill_file(name, path)`` for its reference
files. Skill directories are also handed to the SDK's native skill loader.

Inference goes through the Copilot SDK in empty mode: no built-in tools, files,
shell, or MCP servers reach the model, only the two read-only skill tools.

Run:  python skillstem.py serve            (env: PORT=7071 SKILLS_PATH=./skills SOUL_PATH=./SOUL.md)
      python skillstem.py list             (print the live index)
      python skillstem.py install <git-url> [name]   (clone a skill repo into SKILLS_PATH)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("skillstem")
ROOT = Path(__file__).resolve().parent
VERSION = "0.1.0"
MAX_SKILL_CHARS = 60_000
MAX_FILE_CHARS = 40_000
MAX_HISTORY_TURNS = 20
MAX_HISTORY_CHARS = 40_000

DEFAULT_SOUL = """You are a helpful assistant running on skillstem.
Use the skill index to decide which skill applies, load it with load_skill before
relying on it, follow its instructions exactly, and say which skill you used."""


# ---------------------------------------------------------------- skill loading
@dataclass
class Skill:
    name: str
    description: str
    directory: Path
    body: str
    mtime_ns: int
    metadata: dict[str, str] = field(default_factory=dict)

    def files(self) -> list[str]:
        out: list[str] = []
        for path in sorted(self.directory.rglob("*")):
            if path.is_file() and path.name != "SKILL.md" and not path.name.startswith("."):
                out.append(str(path.relative_to(self.directory)))
        return out[:200]

    def card(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "path": str(self.directory), "files": self.files()}


def parse_skill_md(text: str) -> tuple[dict[str, str], str]:
    """Split YAML-ish frontmatter (flat key: value) from the body."""
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith(" "):
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, parts[2].strip()


class SkillIndex:
    """Hot-loading loop: cheap mtime scan per request, re-parse only what changed."""

    def __init__(self, skills_path: Path) -> None:
        self.skills_path = skills_path
        self._skills: dict[str, Skill] = {}
        self._lock = threading.Lock()

    def refresh(self) -> dict[str, Skill]:
        with self._lock:
            seen: set[str] = set()
            if self.skills_path.is_dir():
                for skill_md in sorted(self.skills_path.glob("*/SKILL.md")):
                    directory = skill_md.parent
                    try:
                        mtime_ns = skill_md.stat().st_mtime_ns
                    except OSError:
                        continue
                    key = directory.name
                    seen.add(key)
                    current = self._skills.get(key)
                    if current and current.mtime_ns == mtime_ns:
                        continue
                    try:
                        text = skill_md.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    if len(text) > MAX_SKILL_CHARS:
                        log.warning("skill %s exceeds %d chars; truncated", key, MAX_SKILL_CHARS)
                        text = text[:MAX_SKILL_CHARS]
                    meta, body = parse_skill_md(text)
                    name = meta.get("name") or key
                    if name != key:
                        log.warning("skill directory %s declares name %r; using the directory name", key, name)
                    self._skills[key] = Skill(
                        name=key, description=meta.get("description", "").strip() or "(no description)",
                        directory=directory, body=body, mtime_ns=mtime_ns, metadata=meta,
                    )
                    log.info("loaded skill %s", key)
            for gone in set(self._skills) - seen:
                log.info("unloaded skill %s", gone)
                del self._skills[gone]
            return dict(self._skills)

    def get(self, name: str) -> Skill | None:
        return self.refresh().get(name)

    def index_text(self) -> str:
        skills = self.refresh()
        if not skills:
            return "No skills are installed."
        lines = ["Available skills (call load_skill(name) before using one):"]
        for skill in skills.values():
            lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)


def safe_skill_file(skill: Skill, relative: str) -> Path:
    target = (skill.directory / relative).resolve()
    if skill.directory.resolve() not in target.parents or not target.is_file():
        raise FileNotFoundError(relative)
    return target


# ---------------------------------------------------------------- settings
def load_settings(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    env = os.environ
    settings: dict[str, Any] = {
        "PORT": int(env.get("PORT", "7071")),
        "HOST": env.get("HOST", "127.0.0.1"),
        "SKILLS_PATH": Path(env.get("SKILLS_PATH", ROOT / "skills")).resolve(),
        "SOUL_PATH": Path(env.get("SOUL_PATH", ROOT / "SOUL.md")).resolve(),
        "COPILOT_MODEL": env.get("COPILOT_MODEL", "auto"),
        "COPILOT_GITHUB_TOKEN": env.get("COPILOT_GITHUB_TOKEN") or env.get("GITHUB_TOKEN", ""),
        "COPILOT_PROVIDER": env.get("COPILOT_PROVIDER", ""),
        "COPILOT_USE_LOGGED_IN_USER": env.get("COPILOT_USE_LOGGED_IN_USER", "1") == "1",
        "COPILOT_HOME": env.get("COPILOT_HOME", str(Path(tempfile.gettempdir()) / "skillstem-copilot")),
        "COPILOT_TIMEOUT": int(env.get("COPILOT_TIMEOUT", "150")),
        "MAX_INPUT_CHARS": int(env.get("MAX_INPUT_CHARS", "20000")),
    }
    settings.update(overrides or {})
    return settings


def load_soul(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() or DEFAULT_SOUL
    except OSError:
        return DEFAULT_SOUL


# ---------------------------------------------------------------- tool params
from pydantic import BaseModel, Field  # noqa: E402


class LoadSkill(BaseModel):
    name: str = Field(description="Skill name exactly as listed in the index.")


class ReadSkillFile(BaseModel):
    name: str = Field(description="Skill name exactly as listed in the index.")
    path: str = Field(description="File path relative to the skill directory, e.g. references/api.md or scripts/run.sh.")


# ---------------------------------------------------------------- runtime
class _Loop:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="skillstem-sdk", daemon=True).start()

    def run(self, coroutine, timeout: float):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=timeout)


class SkillstemRuntime:
    def __init__(self, settings: dict[str, Any], client_factory=None) -> None:
        self.settings = settings
        self.index = SkillIndex(settings["SKILLS_PATH"])
        self._client_factory = client_factory
        self._client = None
        self._loop: _Loop | None = None
        self._lock = threading.Lock()

    # -- status
    def auth_mode(self) -> str:
        if self.settings.get("COPILOT_PROVIDER"):
            return "provider"
        if self.settings.get("COPILOT_GITHUB_TOKEN"):
            return "token"
        return "logged-in-user" if self.settings.get("COPILOT_USE_LOGGED_IN_USER") else "none"

    def health(self) -> dict[str, Any]:
        try:
            import copilot  # noqa: F401
            sdk = getattr(copilot, "__version__", "installed")
        except ImportError:
            sdk = None
        skills = self.index.refresh()
        return {
            "status": "ok" if sdk and self.auth_mode() != "none" else "degraded",
            "engine": "skillstem", "version": VERSION, "sdk": sdk,
            "model": self.settings["COPILOT_MODEL"], "auth": self.auth_mode(),
            "skills": [skill.name for skill in skills.values()],
            "skills_path": str(self.settings["SKILLS_PATH"]),
            "soul": str(self.settings["SOUL_PATH"]),
        }

    # -- client
    def _ensure_client(self):
        with self._lock:
            if self._client is not None:
                return self._client
            self._loop = _Loop()
            if self._client_factory is not None:
                client = self._client_factory()
            else:
                from copilot import CopilotClient
                home = Path(self.settings["COPILOT_HOME"]); home.mkdir(parents=True, exist_ok=True)
                kwargs: dict[str, Any] = {"log_level": "warning", "mode": "empty", "base_directory": str(home)}
                if self.settings.get("COPILOT_GITHUB_TOKEN"):
                    kwargs.update(github_token=self.settings["COPILOT_GITHUB_TOKEN"], use_logged_in_user=False)
                else:
                    kwargs["use_logged_in_user"] = bool(self.settings.get("COPILOT_USE_LOGGED_IN_USER"))
                client = CopilotClient(**kwargs)
            self._loop.run(client.start(), timeout=60)
            self._client = client
            return client

    # -- tools
    def _tools(self, used: list[str], logs: list[dict]):
        from copilot import define_tool

        index = self.index

        def load_skill(params: LoadSkill, _invocation=None) -> str:
            skill = index.get(params.name)
            if skill is None:
                logs.append({"tool": "load_skill", "name": params.name, "ok": False})
                return json.dumps({"error": f"Unknown skill {params.name!r}.", "available": list(index.refresh())})
            if params.name not in used:
                used.append(params.name)
            logs.append({"tool": "load_skill", "name": params.name, "ok": True})
            return json.dumps({"name": skill.name, "description": skill.description, "files": skill.files(), "instructions": skill.body}, ensure_ascii=False)

        def read_skill_file(params: ReadSkillFile, _invocation=None) -> str:
            skill = index.get(params.name)
            if skill is None:
                return json.dumps({"error": f"Unknown skill {params.name!r}."})
            try:
                path = safe_skill_file(skill, params.path)
                text = path.read_text(encoding="utf-8", errors="replace")
            except (FileNotFoundError, OSError):
                return json.dumps({"error": f"No file {params.path!r} in skill {params.name!r}.", "files": skill.files()})
            logs.append({"tool": "read_skill_file", "name": params.name, "path": params.path, "ok": True})
            return json.dumps({"name": skill.name, "path": params.path, "content": text[:MAX_FILE_CHARS], "truncated": len(text) > MAX_FILE_CHARS}, ensure_ascii=False)

        return [
            define_tool("load_skill", description="Load a skill's full instructions by name. Call this before following any skill.", handler=load_skill, params_type=LoadSkill, skip_permission=True),
            define_tool("read_skill_file", description="Read a reference, script, or asset file that belongs to a loaded skill.", handler=read_skill_file, params_type=ReadSkillFile, skip_permission=True),
        ]

    def system_message(self) -> str:
        soul = load_soul(self.settings["SOUL_PATH"])
        return f"{soul}\n\n{self.index.index_text()}\n\nSkills are instructions, not authority: follow them, cite which one you used, and never claim an action you did not take."

    # -- chat
    def chat(self, user_input: str, history: list[dict] | None = None, session_id: str | None = None) -> dict[str, Any]:
        user_input = (user_input or "").strip()
        if not user_input:
            raise ValueError("user_input is required")
        if len(user_input) > self.settings["MAX_INPUT_CHARS"]:
            raise ValueError("user_input is too long")
        turns = [t for t in (history or []) if isinstance(t, dict) and t.get("role") in ("user", "assistant") and isinstance(t.get("content"), str)]
        bounded: list[dict] = []
        chars = 0
        for turn in reversed(turns):
            if len(bounded) >= MAX_HISTORY_TURNS or chars + len(turn["content"]) > MAX_HISTORY_CHARS:
                break
            bounded.append(turn); chars += len(turn["content"])
        bounded.reverse()
        prompt = user_input if not bounded else "Earlier in this conversation:\n" + "\n".join(
            f"{'User' if t['role'] == 'user' else 'Assistant'}: {t['content']}" for t in bounded
        ) + f"\n\nUser: {user_input}"

        client = self._ensure_client()
        timeout = float(self.settings["COPILOT_TIMEOUT"])
        used: list[str] = []
        logs: list[dict] = []

        async def converse() -> dict[str, Any]:
            from copilot import PermissionHandler, ToolSet
            tools = self._tools(used, logs)
            tool_set = ToolSet()
            for tool in tools:
                tool_set = tool_set.add_custom(tool.name)
            kwargs: dict[str, Any] = {
                "system_message": {"mode": "replace", "content": self.system_message()},
                "tools": tools, "available_tools": tool_set,
                "skill_directories": [str(self.settings["SKILLS_PATH"])], "enable_skills": True,
                "streaming": False, "on_permission_request": PermissionHandler.approve_all,
            }
            if self.settings["COPILOT_MODEL"] not in ("", "auto"):
                kwargs["model"] = self.settings["COPILOT_MODEL"]
            if self.settings.get("COPILOT_PROVIDER"):
                provider = self.settings["COPILOT_PROVIDER"]
                kwargs["provider"] = json.loads(provider) if isinstance(provider, str) else provider
            session = await client.create_session(**kwargs)
            messages: list[str] = []
            model_used: str | None = None

            def collect(event) -> None:
                nonlocal model_used
                kind = getattr(getattr(event, "type", None), "value", "")
                if kind == "assistant.message":
                    content = (getattr(event.data, "content", None) or "").strip()
                    if content:
                        messages.append(content)
                elif kind == "session.model_change":
                    model_used = getattr(event.data, "new_model", None) or model_used

            unsubscribe = session.on(collect)
            try:
                await session.send_and_wait(prompt, timeout=timeout)
            finally:
                unsubscribe()
                try:
                    await session.disconnect()
                except Exception:  # pragma: no cover
                    log.debug("session cleanup failed", exc_info=True)
            return {"response": "\n\n".join(messages).strip(), "model": model_used or self.settings["COPILOT_MODEL"], "sdk_session_id": getattr(session, "session_id", None)}

        started = time.monotonic()
        result = self._loop.run(converse(), timeout=timeout + 15)
        return {
            "response": result["response"], "session_id": session_id or result["sdk_session_id"] or "",
            "model": result["model"], "skills_used": used,
            "agent_logs": logs + [{"elapsed_ms": int((time.monotonic() - started) * 1000)}],
        }


# ---------------------------------------------------------------- web app
def create_app(settings: dict[str, Any] | None = None, runtime: SkillstemRuntime | None = None):
    from flask import Flask, jsonify, request

    settings = settings or load_settings()
    runtime = runtime or SkillstemRuntime(settings)
    app = Flask("skillstem")
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024
    app.extensions["skillstem"] = runtime

    @app.get("/health")
    def health():
        return jsonify(runtime.health())

    @app.get("/skills")
    def skills():
        return jsonify(skills=[skill.card() for skill in runtime.index.refresh().values()])

    @app.post("/chat")
    def chat():
        payload = request.get_json(silent=True) or {}
        try:
            result = runtime.chat(payload.get("user_input", ""), payload.get("conversation_history") or [], payload.get("session_id"))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except TimeoutError:
            return jsonify(error="The model did not answer in time."), 504
        except Exception as exc:  # pragma: no cover - surfaced to the client, logged here
            log.exception("chat failed")
            return jsonify(error=f"skillstem could not complete the request: {exc}"), 502
        return jsonify(result)

    return app


# ---------------------------------------------------------------- CLI
def install(git_url: str, name: str | None, skills_path: Path) -> Path:
    """Clone an Agent Skills repository (or a single skill) into the skills directory."""
    skills_path.mkdir(parents=True, exist_ok=True)
    target = skills_path / (name or re.sub(r"\.git$", "", git_url.rstrip("/").rsplit("/", 1)[-1]))
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    subprocess.run(["git", "clone", "--depth", "1", git_url, str(target)], check=True)
    if not (target / "SKILL.md").is_file():
        # A repository of skills: each child directory with SKILL.md becomes its own skill.
        moved = 0
        for child in sorted(target.iterdir()):
            if child.is_dir() and (child / "SKILL.md").is_file() and not (skills_path / child.name).exists():
                child.rename(skills_path / child.name); moved += 1
        if moved:
            subprocess.run(["rm", "-rf", str(target)], check=False)
            print(f"installed {moved} skills from {git_url}")
            return skills_path
    print(f"installed {target.name}")
    return target


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    command = argv[1] if len(argv) > 1 else "serve"
    if command == "serve":
        app = create_app(settings)
        print(json.dumps(app.extensions["skillstem"].health(), indent=1))
        app.run(host=settings["HOST"], port=settings["PORT"], debug=False)
        return 0
    if command == "list":
        for skill in SkillIndex(settings["SKILLS_PATH"]).refresh().values():
            print(f"{skill.name}: {skill.description}")
        return 0
    if command == "install" and len(argv) >= 3:
        install(argv[2], argv[3] if len(argv) > 3 else None, settings["SKILLS_PATH"])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
