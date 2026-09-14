# skillstem

A brainstem-compatible agent harness on the **GitHub Copilot SDK** whose only
extension point is **`SKILL.md`**. Drop an [Agent Skills](https://agentskills.io)
directory into `skills/`, and it is live on the next request. No agent modules,
no plugin registry, no restart.

```
skills/
  hello-world/
    SKILL.md          <- name + description frontmatter, instructions in the body
    references/       <- optional files the model can read on demand
    scripts/          <- optional; skillstem never executes them, it only reads them
```

## Why

The RAPP brainstem hot-loads `agent.py` files. The rest of the industry converged on
`SKILL.md`: one directory per capability, YAML frontmatter, progressive disclosure.
skillstem keeps the brainstem's wire contract so existing clients keep working, and
swaps the extension model for skills so any public skill repository is usable as is.

## Run

```bash
pip install -r requirements.txt
python skillstem.py serve            # http://127.0.0.1:7071
python skillstem.py list             # print the live skill index
python skillstem.py install https://github.com/<org>/<skills-repo>   # clone skills in
```

Environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT`, `HOST` | `7071`, `127.0.0.1` | Bind address |
| `SKILLS_PATH` | `./skills` | Directory scanned for `*/SKILL.md` on every request |
| `SOUL_PATH` | `./SOUL.md` | Persona and standing rules, prepended to every system message |
| `COPILOT_MODEL` | `auto` | Any model id from `copilot` (`gpt-6-astra`, `claude-opus-5`, ...) |
| `COPILOT_GITHUB_TOKEN` | | Copilot-enabled token for servers; locally the signed-in Copilot user is used |
| `COPILOT_PROVIDER` | | JSON bring-your-own-key provider config (Azure OpenAI, OpenAI, Anthropic) |
| `COPILOT_HOME` | temp dir | Private storage for the SDK runtime |

## Wire contract

```
POST /chat   {"user_input": "...", "conversation_history": [{"role":"user","content":"..."}], "session_id": "..."}
          -> {"response": "...", "session_id": "...", "model": "...", "skills_used": ["hello-world"], "agent_logs": [...]}
GET  /health -> {"status": "ok", "engine": "skillstem", "skills": [...], "soul": "...", "model": "..."}
GET  /skills -> {"skills": [{"name","description","path","files"}]}
```

## How a request runs

1. **Hot-load.** `SKILLS_PATH` is scanned; changed `SKILL.md` files are re-parsed by
   mtime, new directories appear, deleted ones vanish. Nothing is imported or run.
2. **System message.** `SOUL.md` + a one-line index of every skill
   (`- name: description`). That is all the model sees up front.
3. **Tools.** Exactly two, both read-only: `load_skill(name)` returns a skill's full
   instructions and file list; `read_skill_file(name, path)` returns a reference file.
   The SDK session runs in *empty mode*, so no shell, filesystem, MCP, or built-in
   tools are reachable. `skills/` is also passed to the SDK's native skill loader.
4. **Answer.** The assistant text, the model used, which skills were loaded, and a
   tool log come back in the brainstem shape.

## Security posture

- Skills are instructions and data, never code. `scripts/` are readable, not runnable.
- Paths are confined to the skill's directory; traversal is refused.
- Empty-mode sessions: the model cannot touch the host beyond the two tools.
- History is bounded (20 turns / 40k chars); input is capped.

## Tests

```bash
python -m pytest -q
```

The tests use a fake SDK client, so they run offline. A live smoke test needs a
signed-in `copilot` CLI or a `COPILOT_GITHUB_TOKEN`.

## License

MIT
