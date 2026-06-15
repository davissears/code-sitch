# Claude Code · Situation Monitor

A local web dashboard of **every Claude Code conversation on this machine**. Each
session is one row: an AI-generated summary, when it was last touched, and whether
it is **running right now**. Search instantly by keyword, or press **Enter** to let
Claude agentically find the conversations you mean. Click an **active** session to
focus its terminal window; click an **inactive** one to resume it in a fresh
terminal. A usage table shows chats / messages sent / tokens for today, the last 7
days, the last 30 days, and all time.

And you can take it with you: served over a private network (Tailscale), the
dashboard works from your phone, where every session opens a **chat view** — read
what each Claude is doing, send it new prompts, and watch replies arrive, all
against the *same* live terminal session on your Mac. Messages sent from the phone
appear in the terminal scrollback, so you sit back down at the laptop exactly where
the conversation is. Setup and security model: **[REMOTE.md](REMOTE.md)**.

No build step and no dependencies — Python standard library on the backend, vanilla
JS on the frontend.

> **Platform: macOS only.** "Focus window" and "Resume" drive **Apple Terminal**
> (Terminal.app) via AppleScript, and optionally **yabai** to cross Mission Control
> spaces. Other terminals are detected but can't be focused yet (see Troubleshooting).

---

## Requirements

| Requirement | Why it's needed | Required? |
|---|---|---|
| **macOS** | Uses AppleScript (`osascript`), `ps`, and `lsof`. Built and tested on macOS 26 / Terminal.app. | **Yes** |
| **Python 3.9+** | Runs the server. **Standard library only — nothing to `pip install`.** | **Yes** |
| **Claude Code transcripts** under `~/.claude/projects/` | The data source. The dashboard reads these `*.jsonl` logs. If you've never run Claude Code interactively, the list will be empty. | **Yes** (for data) |
| **`claude` CLI**, installed and authenticated | Powers AI keyword generation and the Enter-to-search. Everything else works without it. | Optional |
| **Apple Terminal** (Terminal.app) | "Focus window" / "Resume" use Terminal AppleScript. Sessions running under other terminals appear in the list but can't be focused/resumed yet. | For focus/resume |
| **yabai** (`brew install koekeishiya/formulae/yabai`) | Lets "focus" switch to a window on another Mission Control space. Without it, focus still works within the current space. | Optional |

### Preflight checks (read-only, safe to run)

```bash
sw_vers                      # macOS version (Darwin 25+/macOS 26 tested)
python3 --version            # 3.9 or newer
command -v claude            # the claude CLI (optional — enables AI features)
ls -d ~/.claude/projects     # transcript store; must exist and contain *.jsonl
command -v yabai             # optional — cross-Space focus
```

If `~/.claude/projects` is missing, run any interactive `claude` session once to
create it. If `command -v claude` prints nothing, the AI keyword/search features
are disabled but the rest of the app works.

---

## Install & run

```bash
git clone https://github.com/JeremyIV/claude-code-situation-monitor.git
cd claude-code-situation-monitor
./run.sh                     # starts the server on :8787 and opens your browser
```

There is nothing to install — no package manager, no virtualenv. If the browser
doesn't open automatically, visit <http://127.0.0.1:8787/>.

To run it directly (e.g. headless, or to see logs in your terminal):

```bash
python3 server.py            # serves http://127.0.0.1:8787/ ; Ctrl-C to stop
```

### Run automatically at login (optional)

Install a macOS **LaunchAgent** that starts the monitor at login and restarts it if
it crashes. The script generates a plist with the correct paths for *your* machine —
nothing is hardcoded:

```bash
./install-startup.sh         # writes ~/Library/LaunchAgents/com.claude-situation-monitor.plist and loads it
./uninstall-startup.sh       # stops and removes it
```

It logs to `CSM_STATE_DIR/server.log`. The **first** time it focuses or
resumes a window after a login, macOS may ask to let it **control Terminal**
(Automation) — approve once. If you use yabai, grant it Accessibility permission too.

---

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `CSM_PORT` | `8787` | Port to serve on. |
| `CSM_HOST` | `127.0.0.1` | Bind address. Keep localhost unless you're setting up phone access — then follow [REMOTE.md](REMOTE.md) (private network + access token; never the open internet). |
| `CSM_PROVIDERS` | `claude,codex` | Comma-separated providers to index. |
| `CSM_STATE_DIR` | `~/.claude/situation-monitor` if it exists, otherwise `~/.situation-monitor` | Cache, logs, and remote token directory. |
| `CSM_CLAUDE_HOME` | `~/.claude` | Claude Code home directory; transcripts are read from `projects/` under it. |
| `CSM_CODEX_HOME` | `~/.codex` | Codex home directory. |
| `CSM_CODEX_STATE_DB` | `~/.codex/sqlite/state_5.sqlite` | Codex thread state database. |
| `CSM_CLAUDE_CLI` | `claude` | Claude CLI command used for AI search/keywords and Claude resume. |
| `CSM_CODEX_CLI` | `codex` | Codex CLI command used for Codex resume. |
| `CSM_ENRICH_LIMIT` | `50` | How many of the newest sessions get AI keywords per background pass. |

Example: `CSM_PORT=9000 ./run.sh`

The **"skip permissions on resume"** checkbox (top-right of the UI) launches resumed
sessions with `--dangerously-skip-permissions`. It is **on by default**; uncheck it
to resume with normal permission prompts. The choice persists in the browser.

---

## How it works

Claude Code writes one JSONL transcript per conversation under
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. The monitor reads those.

| Concern | Approach |
|---|---|
| **Summary** | Claude Code already writes an `aiTitle` per session; we use it (falling back to the first prompt). |
| **Last updated** | The transcript file's mtime. |
| **Usage stats** | Per-message token usage and prompt counts are read from each transcript's `usage` blocks (deduped by `requestId`), bucketed by local day, then summed into a **Today / Last 7 days / Last 30 days / All time** table of chats, messages sent, and tokens. Windows are *rolling* so they stay monotonic. The headline token number is input + cache-writes + output; cheap cache *reads* (context replays) appear only in the hover tooltip. |
| **Active vs. inactive** | We list live `claude` processes (`ps`), resolve each one's working directory (`lsof`) and controlling tty, then match it to the newest session in that directory. A session with a live process is *active*. |
| **Working vs. waiting** | For each active session we read its Terminal tab title (via AppleScript). Claude Code prefixes the title with an animated Braille spinner (`⠂⠄⠆…`) while it's generating and a steady `✳` when it's done and waiting for you — so the leading glyph *is* Claude's own busy state, and we just read it back. Shown as a green **working** dot vs. an amber **waiting** dot. Keyed by tty (authoritative from `ps`), so it's correct even when several sessions share one directory. Falls back to a plain **active** badge on non-Terminal terminals. |
| **Instant keyword search** | Filters locally as you type over title, project, prompts, and an AI-generated keyword pool. |
| **AI keyword pool** | Generated per session by `claude -p` in the background and cached in `CSM_STATE_DIR`. |
| **Enter → agentic search** | Hands the whole session index to `claude -p`, which ranks by intent and explains each match. |
| **Focus window** (active) | Finds the Terminal tab whose tty matches the live process, selects it, and hands off to **yabai** to cross Mission Control spaces. |
| **Resume** (inactive) | Opens a new Terminal window, `cd`s to the session's original directory, and runs `claude --resume <id>`. |
| **Remote chat** (`/chat`) | Reading: tails the session transcript incrementally by byte offset. Writing: AppleScript types your message into the live TUI via its Terminal tab (keyed by tty) and submits it — the same session, same scrollback, nothing forks. Tool calls render as compact chips; working/waiting status rides along. |
| **Auth** (remote mode) | If `CSM_STATE_DIR/token` (or `CSM_TOKEN`) exists, every request needs it — login page sets a year-long HttpOnly cookie; `Authorization: Bearer` works for scripts; constant-time compares. No token → localhost-only behavior, no login. |

Headless `claude -p` runs (`entrypoint: "sdk-cli"`) are excluded from the list, and
the monitor's own AI calls use `--no-session-persistence`, so it never indexes itself.

---

## Project layout

- `server.py` — stdlib HTTP server + JSON API (`/api/sessions`, `/api/chat`, `/api/chat/send`, `/api/login`, `/api/focus`, `/api/resume`, `/api/search`, `/api/refresh`)
- `sessions.py` — discover + parse transcripts (single-pass, mtime-cached, usage aggregation)
- `liveness.py` — detect which sessions are running
- `windows.py` — focus (AppleScript + yabai) / resume (AppleScript)
- `bridge.py` — remote chat: transcript→messages parsing + typing into the live TUI
- `keywords.py` — heuristic + AI keyword pools, background enricher
- `agentic.py` — Enter-key agentic relevance search
- `claude_cli.py` — headless `claude -p` wrapper + connectivity probe
- `static/` — the UI (`index.html`/`app.js`/`style.css`, chat: `chat.html`/`chat.js`/`chat.css`, `login.html`, `favicon.svg`)
- `run.sh` — start the server and open the browser
- `install-startup.sh` / `uninstall-startup.sh` — optional login-time LaunchAgent
- `REMOTE.md` — phone access: Tailscale setup, token auth, threat model

---

## Offline behavior

The session list, instant keyword filter, live status, **focus**, and **resume** are
entirely local and keep working with no internet. Only two features use the Anthropic
API, and they degrade cleanly:

- **AI keyword enrichment** pauses while offline and resumes automatically when the
  connection returns (already-generated keywords stay cached and searchable).
- **Agentic search (Enter)** returns immediately with a friendly "you're offline" note
  instead of hanging; the instant filter still covers everything.

An **AI offline** chip appears in the header when the API is unreachable. The app
probes reachability cheaply (cached ~10s) so it never spawns doomed calls.

---

## Troubleshooting

- **The list is empty.** You have no transcripts under `~/.claude/projects/`, or they
  are all headless (`entrypoint: sdk-cli`, which are excluded by design). Run an
  interactive `claude` session and the list refreshes within a few seconds.
- **Port already in use.** Another instance (or the LaunchAgent) is already serving.
  Use `CSM_PORT=9000 ./run.sh`, or stop the other one.
- **"Focus" does nothing / detail says "no Terminal tab found".** The session is not
  running under Apple Terminal (iTerm, Ghostty, WezTerm, etc. aren't supported for
  focus yet), **or** macOS hasn't granted Automation permission — check System
  Settings → Privacy & Security → Automation and allow control of Terminal.
- **Focus doesn't switch Mission Control Spaces.** Install `yabai` and grant it
  Accessibility permission. Without yabai, focus only works within the current Space.
- **AI keywords / search show "offline".** `claude` isn't reachable (no network, or not
  authenticated). Test with `claude -p hello`. Local features keep working regardless.
- **The login service won't stay running.** Read `CSM_STATE_DIR/server.log`.
  A clean "port already in use" exit is expected if you also ran `./run.sh` manually.

---

## Security & privacy

Runs on `127.0.0.1` by default. It reads your Claude Code transcripts locally and can
focus or resume terminal windows — and, in remote mode, type into your live Claude
sessions — so never put it behind a public proxy or port-forward it to the internet.
Phone access goes through a private network (Tailscale) plus a mandatory access
token; the full threat model and setup are in [REMOTE.md](REMOTE.md). It does not
send your transcripts anywhere except, for the optional AI features, to the
Anthropic API via your own authenticated `claude` CLI.

---

## License

MIT — see [LICENSE](LICENSE).
