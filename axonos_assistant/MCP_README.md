# MCP Integration for AxonAI

This document describes how AxonAI (`axonos_assistant/`) is put together today:
which backend answers a turn, how turns are routed, what the Model Context
Protocol (MCP) pieces in this directory actually do, and how to install, run,
and test the assistant.

Short version: AxonAI is a GTK desktop application whose default backend is a
local **OpenCode** agent driving **`ollama/qwen3.8:latest`**. MCP is a
**context sidecar** that feeds a system summary into the tool-free direct chat
mode. The `mcp_*_server.py` scripts in this directory are complete FastMCP
servers, but nothing in the repository currently launches them or opens an MCP
session to them.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            AxonAI (main.py)                             │
│  ┌──────────────┐ ┌────────────────────┐ ┌───────────────┐ ┌──────────┐ │
│  │  GTK 3 UI    │ │ assistant_routing  │ │ opencode_     │ │ mcp_     │ │
│  │  WebKit2     │ │ choose_route()     │ │ client.py     │ │ client.py│ │
│  └──────────────┘ └────────────────────┘ └───────┬───────┘ └────┬─────┘ │
└──────────────────────────────────────────────────┼──────────────┼───────┘
                        agent / vision (agent on)  │              │ chat
                                                   ▼              ▼
┌──────────────────────────────────────┐   ┌──────────────────────────────┐
│ OpenCode `serve` 127.0.0.1:4096      │   │ Ollama /api/generate         │
│ user aXonian, config from            │   │ localhost:11434              │
│ /etc/axonos-opencode/opencode.json   │   │ qwen3.8:latest, no tools,    │
│ tools: read/glob/list/grep/edit/bash │   │ prompt carries the MCP       │
│ approvals via GTK dialogs            │   │ context summary              │
└──────────────────┬───────────────────┘   └──────────────────────────────┘
                   ▼
        Ollama OpenAI-compatible API  http://127.0.0.1:11434/v1
```

### Routes

Every turn is routed exactly once by `assistant_routing.choose_route(text,
agentic_enabled)` (`assistant_routing.py`, called from `main.py`
`route_query`). The rules are deterministic:

1. A leading `/agent`, `/chat`, or `/vision` prefix forces that route and is
   stripped from the message (`ROUTE_OVERRIDES`).
2. Otherwise, if the text contains any phrase in `VISION_PHRASES`
   ("what do you see", "describe the screen", "what windows are open",
   "explain this chart", ...), the route is `vision`.
3. Otherwise the route is `agent` when *Use OpenCode agent mode by default*
   is enabled (the default), else `chat`.

| Route    | Backend                                                    | Notes |
|----------|------------------------------------------------------------|-------|
| `agent`  | OpenCode session at `http://127.0.0.1:4096`, model `ollama/qwen3.8:latest` | Default. Persistent session per conversation; tool calls governed by `opencode.json`; `ask` permissions surface as GTK approval dialogs (Allow once / Always until OpenCode restarts / Deny); OpenCode questions surface as GTK question dialogs. |
| `chat`   | Direct Ollama `http://localhost:11434/api/generate`, streaming | Tool-free. `build_prompt()` prepends `## CURRENT SYSTEM CONTEXT (Real-time via MCP)` from `MCPClientManager.get_context_summary()` plus the last two exchanges. |
| `vision` | Screenshot (root-window capture, downscaled to fit 1344x1344) attached to the turn | Goes through OpenCode when agent mode is on, otherwise through direct Ollama with the image. |

Direct-chat turns that happened outside the OpenCode session are bridged into
the next agent turn by `format_agent_request()`; cancelled turns are never
bridged. There is no keyword-based "system query" dispatcher: system, process,
and application-launch questions are answered by OpenCode tools in agent mode
or by the model reading the MCP summary in chat mode.

### Routing UI setting

The settings dialog (gear button) exposes:

- **Multimodal Model** — text entry, default `qwen3.8:latest`; used for both
  OpenCode (`providerID: ollama`) and direct Ollama.
- **Use OpenCode agent mode by default** — checkbox bound to
  `agentic_enabled`. The header badge shows `LOCAL · AGENTIC` or
  `LOCAL · CHAT` accordingly.

Both values live in memory for the running process only.

## MCP context sidecar (`mcp_client.py`)

`MCPClientManager` is what `main.py` initialises at startup
(`initialize_mcp_async`). What it does:

- Discovers the three server scripts by probing `dirname(__file__)`,
  `/opt/axonos_assistant`, and `/home/avi/AxonOS/axonos_assistant`, and
  records a stdio launch spec (`python3 <script>` with `PYTHONPATH` set) for
  each one found.
- **Does not connect to them.** `_connect_to_server` only logs
  `Would connect to MCP server: ...`; `self.sessions` is never populated and
  the imported `stdio_client` / `ClientSession` are unused.
- Gathers OS context itself, every 2 seconds (`context_update_interval`,
  hard-coded), by running subprocesses: `ps aux --sort=-pcpu` (top 20
  processes), `free`, `df`, network interface inspection, `wmctrl -l` with an
  `xdotool` fallback for window titles, and a process-name scan for known
  scientific applications.
- Exposes `get_os_context()`, `get_context_summary()`,
  `force_memory_update()`, `execute_os_command()`, and `get_file_context()`.

Only `get_context_summary()` is used by the UI (chat route). If
initialisation fails, `mcp_context_enabled` is set to `False` and the summary
degrades to `MCP context disabled`; the assistant keeps working.

## MCP servers

All three servers are FastMCP (`mcp.server.fastmcp`) stdio servers started with
`python3 <script>` and `mcp.run()`. They are self-contained and can be attached
to any MCP client (for example via an `mcp` block in `opencode.json`, which is
not configured today).

### OS Context Server (`mcp_os_server.py`, `FastMCP("AxonOS OS Context Server")`)

Tools:

- `get_system_info() -> SystemInfo` — hostname, platform, architecture, CPU
  count, memory, load average, disk usage, network interfaces.
- `get_top_processes(limit: int = 10) -> List[ProcessInfo]` — by CPU.
- `get_process_by_name(process_name: str) -> List[ProcessInfo]`
- `kill_process(pid: int, signal: int = 15) -> dict` — sends the signal, waits
  up to 3 s.
- `execute_command(command: str, args: List[str] = None, timeout: int = 30) -> dict`
  — `command` must be in the allowlist below; `args` are not filtered.
- `get_desktop_info() -> dict` — reads `DESKTOP_SESSION`, `DISPLAY`,
  `WAYLAND_DISPLAY`, `XDG_SESSION_TYPE`, `XDG_CURRENT_DESKTOP`,
  `WINDOW_MANAGER`; window list via `wmctrl -l` (5 s timeout).
- `launch_application(app_name: str, args: List[str] = None) -> dict` —
  supported names: `jupyter`, `jupyterlab`, `rstudio`, `spyder`, `octave`,
  `qgis`, `ugene`, `fiji`, `imagej`, `firefox`, `thunar`, `terminal`
  (terminator), `calculator` (qalculate-gtk), `texteditor` (mousepad).

`execute_command` allowlist: `ls dir pwd whoami id date uptime free df ps top
htop vmstat iostat netstat ss ip ifconfig ping nslookup git python3 python pip
pip3 conda jupyter jupyter-lab rstudio r octave spyder code gedit nano vi vim
firefox qgis ugene imagej fiji`. Because arguments are unrestricted, entries
such as `python3`, `git`, and `pip` make this list advisory rather than a
security boundary.

Resources: `os://system/info`, `os://processes/top` (top 15),
`os://desktop/environment`, `os://applications/scientific`.

Prompts: `system_analysis_prompt(focus_area: str = "general") -> str`,
`process_troubleshooting_prompt(issue_description: str) -> str`,
`application_launcher_prompt(app_category: str = "scientific") -> List[base.Message]`.

### Filesystem Server (`mcp_filesystem_server.py`, `FastMCP("AxonOS Filesystem Server")`)

Tools:

- `list_directory(path: str = ".") -> DirectoryListing` — unrestricted.
- `get_file_info(path: str) -> FileInfo` — unrestricted.
- `read_file(path: str, encoding: str = "utf-8") -> str`
- `write_file(path: str, content: str, encoding: str = "utf-8") -> dict` —
  creates parent directories.
- `create_directory(path: str) -> dict`
- `delete_file(path: str) -> dict` — recursive for directories.

`read_file`, `write_file`, `create_directory`, and `delete_file` resolve the
path and require it to start with one of `/home`, `/opt/axonos_assistant`,
`/tmp`.

Resources: `fs://current/directory`, `fs://home/directory`.

### Process Manager Server (`mcp_process_server.py`, `FastMCP("AxonOS Process Manager Server")`)

Tools:

- `get_all_processes() -> List[ProcessInfo]`
- `get_top_processes(limit: int = 10, sort_by: str = "cpu") -> List[ProcessInfo]`
- `get_process_by_name(process_name: str) -> List[ProcessInfo]`
- `get_process_by_pid(pid: int) -> ProcessInfo`
- `kill_process(pid: int, signal: int = 15) -> dict`
- `start_process(command: str, args: List[str] = None, background: bool = True) -> dict`
  — foreground mode has a fixed 30 s timeout.
- `get_process_tree(pid: int = None) -> ProcessTree`
- `get_system_resources() -> SystemResources`

`start_process` allowlist: `jupyter jupyter-lab rstudio spyder octave qgis
ugene fiji firefox thunar terminator python3 python r git ls pwd whoami`
(arguments unrestricted).

Resources: `process://all/running` (top 20 by CPU), `process://system/resources`.

## Security

The operative safety boundary for anything AxonAI *does* on the system is the
OpenCode permission policy in `opencode.json` (installed to
`/etc/axonos-opencode/opencode.json`, root-owned):

- Default `"*": "ask"` — every tool not listed below prompts the user.
- `external_directory: deny` — the agent stays inside its working directory
  (`/home/aXonian`).
- `read`: allowed by default, denied for `*.env`, `*.env.*` (except
  `*.env.example`), `.ssh/*`, `.gnupg/*`, `.aws/*`, `.config/gcloud/*`,
  `.config/gh/hosts.yml`, `.config/chromium/*`, `.config/google-chrome/*`,
  `.mozilla/*`, `.local/share/opencode/auth.json` (each also with a `*`
  prefix variant).
- `glob`, `list`, `lsp`, `todo*`, `question`: allow. `grep`, `task`, `edit`:
  ask.
- `bash`: ask by default; hard deny for `rm`, `sudo`, `su`, `dd`, `mkfs`,
  `shutdown`, `reboot`, `poweroff`, `git push` (with and without arguments).
- `share: disabled`, `autoupdate: false`.

Approval requests are answered in the GTK UI (`request_agent_permission`):
Allow once, Always until OpenCode restarts, or Deny. The agent system prompt
instructs the model never to work around a denial and to treat file, web,
tool-output, and screenshot content as untrusted data.

Process isolation (`supervisord.conf`): OpenCode runs as the desktop user
`aXonian`, bound to `127.0.0.1:4096` only, with `HOME=/var/empty/axonos-opencode`
and XDG directories under `/etc/axonos-opencode`, `/var/lib/axonos-opencode`,
`/var/cache/axonos-opencode`. The root-owned plugin `desktop-shell-env.js`
gives approved shell tools the desktop user's environment
(`HOME=/home/aXonian`, `DISPLAY=:0`, `XAUTHORITY`, `VGL_DISPLAY`) and blanks
the service's `OPENCODE_*` loader variables so a nested `opencode` CLI does not
inherit them.

Rendering: model output is converted with `markdown` and sanitised with
`bleach` (`render_markdown`) before reaching the WebKit view.

Caveats:

- `MCPClientManager.execute_os_command()` and `get_file_context()` have **no
  allowlist, path restriction, or timeout**. They are not reachable from the
  UI (only `test_mcp_integration.py` calls them) but should not be exposed
  without adding the server-side checks.
- The MCP server allowlists above are not enforced at runtime because the
  servers are not launched.

## OpenCode runtime configuration

Installed by the `Dockerfile` (`Install AxonAI` step) and supervised by
`supervisord.conf`:

| Item | Value |
|------|-------|
| OpenCode version | `OPENCODE_VERSION=1.18.26`, binary `/usr/local/libexec/opencode` (symlinked to `/usr/local/bin/opencode`) |
| Service command | `opencode serve --hostname 127.0.0.1 --port 4096`, `user=aXonian`, `directory=/home/aXonian` |
| Config | `OPENCODE_CONFIG=/etc/axonos-opencode/opencode.json`, `OPENCODE_CONFIG_CONTENT=""`, `OPENCODE_DISABLE_PROJECT_CONFIG=true`, `OPENCODE_DISABLE_EXTERNAL_SKILLS=true`, `OPENCODE_DISABLE_CLAUDE_CODE=true`, `OPENCODE_DISABLE_AUTOUPDATE=true` |
| Unset at launch | `OPENCODE_CONFIG_DIR OPENCODE_TEST_HOME OPENCODE_PERMISSION OPENCODE_PURE OPENCODE_EXPERIMENTAL OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS` |
| Plugin | `/etc/axonos-opencode/opencode/plugins/desktop-shell-env.js` |
| Provider | `ollama` via `@ai-sdk/openai-compatible`, `baseURL http://127.0.0.1:11434/v1` |
| Model | `qwen3.8:latest` — `attachment`, `reasoning`, `tool_call` enabled; input `text`+`image`; context 65536, output 8192 |
| Agent | `default_agent: build`; `model` and `small_model` both `ollama/qwen3.8:latest` |
| Ollama | `ollama serve` with `OLLAMA_CONTEXT_LENGTH=65536`; `qwen3.8:latest` pulled at image build |
| Safety marker | `/run/axonos-assistant/opencode-active` (+ `.lock`), owned by `aXonian`, cleared by `startup.sh` at session start; serialises OpenCode and direct-Ollama turns across assistant processes |

`opencode_client.py` defaults: `base_url="http://127.0.0.1:4096"`,
`directory="/home/aXonian"`, `marker_wait_timeout=60`, per-turn
`timeout=900`. It talks HTTP + SSE, reconciles session state, forwards
permission and question requests, and fences cancellation so a stopped turn
cannot be completed retroactively.

The only environment variables the MCP code reads are the desktop diagnostics
listed under `get_desktop_info` (see `docs/ENVIRONMENT_VARIABLES.md`).

## Installation

Inside the image (`Dockerfile`):

```bash
COPY axonos_assistant /opt/axonos_assistant
/usr/bin/python3 -m pip install -r requirements.txt
cp axonos-assistant.desktop /usr/share/applications/
```

`requirements.txt`: `requests`, `beautifulsoup4`, `markdown`, `bleach`,
`Pillow`, `mcp>=1.0.0`, `pydantic>=2.0.0`, `psutil>=5.9.0` (`psutil` is used
only by the server scripts). Not covered by pip and required at runtime:

- PyGObject with GTK 3, `Notify` 0.7, and `WebKit2` 4.0 typelibs.
- `xdotool` (installed in the image) — screenshot and window fallback.
- `wmctrl` — preferred window-list source in both `mcp_client.py` and
  `mcp_os_server.py`; not installed by the current `Dockerfile`, so the
  `xdotool` fallback is what runs.
- A running `ollama serve` on `:11434` and `opencode serve` on
  `127.0.0.1:4096` (both supervised).

Always use `/usr/bin/python3`; the conda interpreter is viewer-only and must
not be used for the assistant.

## Running

Desktop entry (`axonos-assistant.desktop`):

```
Exec=/usr/bin/python3 /opt/axonos_assistant/main.py
StartupWMClass=AxonAI
```

The application is single-instance (`Gtk.Application` id
`org.axonos.AxonAI`); launching it again activates the existing window. If
OpenCode is unreachable, agent turns return
"The OpenCode execution backend became unavailable ..." while chat mode keeps
working; if Ollama is unreachable, chat turns report
"Cannot reach Ollama or load qwen3.8:latest".

Logs:

- AxonAI itself prints to its own stdout/stderr (`print` and `logging`;
  `MCPClientManager` forces `logging.basicConfig(level=DEBUG)`, so MCP context
  gathering is already verbose).
- OpenCode and Ollama log through supervisord to the container's
  stdout/stderr (`supervisorctl tail -f opencode`, `supervisorctl tail -f
  ollama`).
- The MCP server scripts log at INFO to stderr when run standalone.

## Tests

From `/home/cluadmin/AxonOS/axonos_assistant` (or the equivalent checkout):

```bash
# Pure routing rules: overrides, vision phrases, history bridging
/usr/bin/python3 -m unittest test_assistant_routing

# OpenCode client: sessions, SSE reduction, cancellation fencing,
# permission/question payloads, cross-process safety marker (mocked HTTP)
/usr/bin/python3 -m unittest test_opencode_client

# Static UI/branding contracts; reads ../talk_to_k, ../startup.sh,
# ../xfce4-panel.xml, ../gtk-tooltip.css, so run inside the full repo
/usr/bin/python3 -m unittest test_ui_contract

# All three at once
/usr/bin/python3 -m unittest test_assistant_routing test_opencode_client test_ui_contract
```

`test_mcp_integration.py` is a standalone script, not a unittest suite:

```bash
/usr/bin/python3 test_mcp_integration.py
```

It requires the `mcp` package to import, exercises only the client-side
subprocess gathering (`get_os_context`, `get_context_summary`), and calls the
unrestricted `execute_os_command("whoami")` and
`get_file_context("/etc/hostname")`. It does not start or test any MCP
server, tool, or schema.

## Troubleshooting

- **Agent turns fail immediately** — check `supervisorctl status opencode`
  and `curl http://127.0.0.1:4096/global/health`; confirm
  `/etc/axonos-opencode/opencode.json` is present and root-owned.
- **Chat turns fail** — `curl http://localhost:11434/api/tags` should list
  `qwen3.8:latest`.
- **"MCP initialization failed" banner** — the `mcp` package failed to import
  or `MCPClientManager` raised; the assistant continues without the context
  summary.
- **Empty or stale system context in chat mode** — see the first pending item
  below; also confirm `ps`, `free`, `df`, and `xdotool`/`wmctrl` are on PATH.
- **Turn appears stuck after Stop** — a previous owner of
  `/run/axonos-assistant/opencode-active` may not have released it; the
  client waits up to 60 s, then reports the marker error. `startup.sh`
  clears the marker at session start.

## Pending work

- `main.py` `initialize_mcp_async` creates a fresh event loop, awaits
  `get_mcp_client_manager()` (which schedules `_context_update_loop` with
  `asyncio.create_task`), then closes the loop. The periodic update task is
  therefore never driven; the context summary seen by chat mode is whatever
  the first synchronous pass produced. Either keep the loop alive in the
  thread or replace the loop with an explicit refresh before each chat turn.
- `mcp_client.py` `_connect_to_server` is a stub; decide whether to open real
  stdio sessions to the three servers (and route context through their tools)
  or retire the server scripts.
- No `mcp` block in `opencode.json`: the servers are not available to the
  OpenCode agent either.
- `MCPClientManager.execute_os_command` / `get_file_context` lack the
  allowlist and path checks the servers implement.
- `execute_command` and `start_process` allowlists do not filter arguments.
- `wmctrl` is called but not installed in the image.
- `mcp_client.py` still probes the host path `/home/avi/AxonOS/axonos_assistant`.
- Dead code in `main.py`: `handle_system_query`, `handle_memory_query`,
  `handle_application_launch`, `handle_help_request` have no callers.
- Model name and agent-mode setting are not persisted across restarts.

## License

This integration is part of AxonAI and follows the same license terms.
