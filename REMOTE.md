# Remote access — chat with your Claudes from your phone

The monitor can be reached from your phone, and every session row opens a
**chat view**: read the conversation (including what Claude is doing right now),
send a new prompt, and watch the reply arrive — all against the *same* session
that's open in a terminal on your Mac. Messages you send from the phone appear
in the terminal scrollback, so when you sit back down at the laptop you pick up
exactly where the conversation is.

**How it works** — no new infrastructure on the sessions themselves:

- *Reading*: every Claude Code conversation is already journaled to a `.jsonl`
  transcript; the chat view tails it (incrementally, by byte offset).
- *Writing*: Terminal.app lets us type into a tab by AppleScript, keyed by the
  session's tty. Your message is literally typed into the live TUI and
  submitted — exactly as if you'd walked to the laptop and typed it.
- *Status*: the working / waiting-for-you indicator comes from the same
  spinner-glyph signal the dashboard already uses.

If a session isn't running, the chat view offers **Resume on laptop**, which
opens a fresh terminal window on the Mac via the existing resume machinery —
then you can chat with it remotely.

---

## Threat model, before anything else

Anyone who can reach this server **can read every transcript and type
arbitrary prompts into Claude sessions that may have `--dangerously-skip-
permissions`**. That is remote code execution on your Mac, full stop. So:

1. **Never port-forward it to the open internet.** No exceptions.
2. Reach it through a **private network (Tailscale — recommended)**, and
3. **Set the access token anyway** (defense in depth — one `openssl` command).

---

## Step 1 — set an access token

```bash
mkdir -p ~/.claude/situation-monitor
openssl rand -hex 32 > ~/.claude/situation-monitor/token
chmod 600 ~/.claude/situation-monitor/token
cat ~/.claude/situation-monitor/token     # copy this — you'll paste it on the phone once
```

When that file exists, every page and API call requires the token. Browsers
get a login screen; after you paste the token once, an HttpOnly cookie keeps
you signed in for a year on that device. (`CSM_TOKEN` env also works, and
`Authorization: Bearer <token>` for scripts.)

To sign out a lost phone: rotate the token (run the `openssl` line again) and
restart the server — every old cookie dies instantly.

## Step 2 — get on a private network (Tailscale)

[Tailscale](https://tailscale.com) is a zero-config WireGuard mesh; the free
plan is plenty. The server stays invisible to the internet — your phone and
laptop just share a private encrypted network wherever they are.

1. On the Mac: `brew install --cask tailscale`, open Tailscale, sign in.
2. On the phone: install the Tailscale app, sign in with the same account.
3. Find the Mac's tailnet IP: `tailscale ip -4` (e.g. `100.x.y.z`).

## Step 3 — bind the server beyond localhost

```bash
CSM_HOST=0.0.0.0 ./install-startup.sh     # or: CSM_HOST=0.0.0.0 ./run.sh
```

Then on your phone open `http://100.x.y.z:8787/`, paste the token, done.
Add it to the home screen (Share → *Add to Home Screen*) and it behaves like
an app.

Stricter variant: bind to the tailnet interface only, so even your LAN can't
see the port: `CSM_HOST="$(tailscale ip -4)" ./install-startup.sh`. (Caveat:
if Tailscale isn't up when the server starts, the bind fails and the
LaunchAgent retries; `0.0.0.0` + token + firewall is the more robust choice.)

The server refuses to be quiet about misconfiguration: bound beyond localhost
with no token, it prints a loud warning at startup.

### Alternative: Cloudflare Tunnel

If you'd rather not install anything on the phone: a `cloudflared` tunnel with
**Cloudflare Access** (email-gated) in front also works and gives you HTTPS on
a real domain. More moving parts; only worth it if you already live in
Cloudflare. Do **not** run a bare tunnel without Access.

---

## Living with it

- **Send from anywhere, finish at the desk.** Phone messages are typed into
  the real terminal; nothing forks, nothing desyncs.
- **Multi-line is fine** — newlines in your message arrive as newlines.
  Messages starting with `/` reach Claude Code's slash-command input, so
  `/compact` from the beach works (power feature; aim carefully).
- **While Claude is working**, a sent message queues in the TUI input
  (exactly like typing ahead at the terminal) and is processed when the
  current turn ends; it shows as a pale "pending" bubble until the transcript
  confirms it.
- **Keep the Mac awake** for remote work: the laptop must be running — clamshell
  with power, `caffeinate`, or Amphetamine. Focus/resume need a logged-in GUI
  session; the screen can be locked for chatting, but resume may require the
  screen to be unlocked to open new Terminal windows.
- Sessions running in terminals other than Apple Terminal (iTerm, Ghostty…)
  are readable remotely but can't receive messages yet.

## Limitations (honest ones)

- A permission prompt or menu open in the TUI consumes whatever you send as
  menu input — with `--dangerously-skip-permissions` sessions this is rare,
  but it's a known sharp edge.
- A brand-new session that has never exchanged a message has no transcript
  yet, so it can't be chatted with until its first local exchange.
- This is one shared token, not user accounts. It's a personal tool; treat the
  token like an SSH key.
