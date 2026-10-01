# Adding a coding agent to `ug`

`ug` core talks to every agent through one interface, `Agent`, defined in
[`src/ucode/agents/interface.py`](../src/ucode/agents/interface.py). Read that file first: it is
short and it is the contract. This guide walks through implementing it for a new agent.

## What you implement

| Member | What core uses it for |
|---|---|
| `display` | The agent's name in messages ("Launching Kilo with Unity Gateway") |
| `install` | `Install(binary, package, ...)`: install, upgrade, `ug doctor`, minimum/maximum version gates |
| `mcp` | Where `ug mcp` registers servers for the agent, or `None` if it can't take any. Required, so "no MCP support" is always a deliberate choice |
| `models(state)` | Which discovered models the agent can use, and the starting model `ug` pins. Drives availability checks, `ug status`, and pickers |
| `configure(state, request)` | Write the agent's `ug`-owned config; return the updated state |
| `launch(state, args, options=...)` | Hand the terminal to the agent |
| `revert(state)` | Undo what `ug` wrote, returning rows for the `ug revert` summary |

Everything else about the agent stays private to its module: config file format, how `ug` keeps
its config isolated from the user's own (a `--settings` flag, a pinned config-dir env var, ...),
how tokens stay fresh, provider IDs, and header workarounds.

## Steps

### 1. Write `src/ucode/agents/<name>.py`

Model it on an existing agent with a similar shape. `opencode.py` is the smallest JSON-config
agent with MCP support; `pi.py` has no MCP; `gemini.py` and `copilot.py` use a dotenv file and a
token-refresh thread.

```python
class KiloMcpClient:
    display = "Kilo"
    binary = "kilo"
    oauth_client_id = None  # no published OAuth app: always use the `ug mcp-proxy` bridge

    def add(self, name, server): ...          # write one entry; return scopes replaced
    def remove(self, name): ...               # return scopes removed from
    def apply(self, add, remove): ...         # many changes in one write; return names removed
    def live_status(self): ...                # parse `kilo mcp list`; {} when unreadable


class KiloAgent:
    display = "Kilo"
    install = Install(binary="kilo", package="@kilocode/cli@7", version_error=minimum_version_error)
    mcp = KiloMcpClient()

    def models(self, state): ...              # project discovered families in `state` onto Kilo
    def configure(self, state, request): ...  # request.model may be None: pick from models()
    def launch(self, state, args, *, options): ...
    def revert(self, state): ...


AGENT = KiloAgent()
```

Rules that keep agents interchangeable:

- **Isolate the agent's config.** Write only `ug`-owned files, and point the agent at them with a
  flag or env var, so the user's own config never merges in and `revert` stays simple. Check by
  hand whether the agent's isolation mechanism *replaces* or *merges with* the user's config;
  several CLIs merge.
- **`models()` must not mutate `state`** and must not call side-effecting helpers.
- **`configure()` raises `RuntimeError` with an actionable message** when it can't configure, for
  example "A kilo model must be selected before configuration."
- **Keep token refresh working past the first hour.** Whatever mechanism the agent supports
  (a refresh plugin, an auth-command helper, a refresh thread), verify a session longer than one
  token lifetime.

### 2. Register it

Add one line to `AGENTS` in `src/ucode/agents/__init__.py`, in the order agents should be listed.
That makes it available to `ug configure --agent`, `ug status`, `ug revert`, `ug doctor`, and
`ug mcp`. Add a `TOOL_ALIASES` entry only if the CLI should accept another spelling.

### 3. Add the `ug <name>` command

Add a launch command in `cli.py` next to the others (`opencode_cmd` is the simplest to copy), and
add the name to `_HELP_COMMAND_ORDER` so it sorts with the other launch commands.

### 4. Tell discovery which model families the agent uses

Today model discovery only stores the families a configure run asks for. If the agent uses
Anthropic, Gemini, Codex, or OSS models, add it to the matching `want_*` checks in
`configure_shared_state` and to `_DISCOVERY_CONSUMERS` in `cli.py`, and to
`_TOOL_DISCOVERY_SOURCES` in `agents/__init__.py` so a failed availability check explains itself.
If you skip this, the agent reports "not available on this workspace" because `models()` sees no
models.

### 5. Opt into features, if the agent supports them

These are per-feature tables. An agent that isn't listed doesn't have the feature, which is the
right default, so only touch the ones you've built and tested:

| Feature | Where |
|---|---|
| Databricks AI Tools install | `AITOOLS_AGENT_TOKENS` in `agents/__init__.py` |
| Launch note about automatic token refresh | the token-refresh note table in `cli.py` |
| Model Provider Services | `_TOOL_PROVIDER_TYPES` in `databricks.py` |
| Admin-managed config | `AGENT_ENUM_TO_TOOL` in `managed_config.py`; needs the backend proto enum first |
| Smart routing, MLflow tracing, OS-managed settings file | Claude Code and Codex only today; see their modules |

### 6. Test it

- `tests/test_agent_interface.py` runs conformance checks over every entry in `AGENTS`
  automatically. Add the module's `restore_file` to its `no_revert_side_effects` fixture so the
  revert check can't touch your real home directory.
- Add `tests/test_agent_<name>.py` with focused tests: what `configure` writes, `models()` and
  the pinned default, `revert` rows, MCP add/remove/apply against a temp config, and launch argv.
  Redirect every config path into `tmp_path`.
- Update `tests/README.md` with what the new file covers.
- Run `uv run pytest` and `just lint`.

### 7. Check it by hand

Against a real workspace:

```bash
uv run ug configure --agent <name>
uv run ug <name>                      # starts, and a prompt gets a response through the gateway
uv run ug <name> -m <model>           # explicit model selection
uv run ug mcp add                     # the agent sees the server
uv run ug status                      # lists the agent's models
uv run ug revert                      # restores the agent's config
```

Also run one session longer than a token lifetime, and confirm the user's own config for the agent
doesn't leak into a `ug` session.

## Growing the interface

If a new agent needs core to do something it can't express through `Agent`, add a member only when
all three are true: every agent needs it for a user-facing command, the answer genuinely differs
between agents, and core can't compute it itself. Otherwise it belongs in a feature table or stays
private to the agent. The same rules are at the top of `interface.py`.
