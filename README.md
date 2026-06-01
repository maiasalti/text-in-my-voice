# text-in-my-voice

A small macOS tool that watches your iMessages and, the moment a new message
arrives, drafts a few reply options **in your own texting voice** using the
Anthropic (Claude) API. It shows the drafts in a live terminal feed so you can
read and copy them.

It learns your voice from **your own sent messages** — there's a script that
extracts them from your Mac and (optionally) has Claude write a "voice profile"
describing how you text. Clone the repo, point it at your own API key and your
own messages, and it drafts as *you*.

> **It never sends anything and never writes to your message database.** Drafting
> is suggestions-only — you stay in control of what actually gets sent.

---

## ⚠️ Privacy — read this first

This tool moves personal text data to a third party (Anthropic). Two separate
flows to be aware of:

1. **Drafting replies** sends the **incoming message + recent thread context** to
   the Claude API every time someone texts you. That means *other people's*
   messages to you leave your machine. Use the `ALLOWLIST` / `BLOCKLIST` at the
   top of `watch.py` to restrict this to people you're comfortable doing that
   for, and exclude sensitive contacts.
2. **Building your voice profile** (`build_voice_profile.py --analyze`) sends a
   sample of **your own** sent messages to the API to analyse your style.

Nothing personal is ever committed to git — your generated `voice/voice-profile.md`
and `voice/examples.md` are `.gitignore`d. Don't run this against conversations
where sending the content to an API isn't OK.

---

## How it works

1. Polls `~/Library/Messages/chat.db` (SQLite, read-only) every couple of seconds
   for new messages, tracking the last row it saw so it only reacts to new ones.
2. When a **received** message arrives, it finds the thread and pulls the last
   ~18 messages for context.
3. It calls Claude with your voice profile + that context and asks for a few short
   reply options — or says "no reply needed" if the message doesn't call for one.
4. It prints who texted, their message, recent context, and the suggestions.
   Optionally copies the top one to the clipboard / posts a notification.

---

## Setup

### 1. Requirements
- A Mac with Messages set up (iMessage in `~/Library/Messages/chat.db`).
- **Full Disk Access** for your terminal app (Terminal.app / iTerm), so it can
  read `chat.db`. System Settings → Privacy & Security → Full Disk Access → add
  your terminal. (Without this you'll get `unable to open database file`.)
- Python 3.9+.
- An Anthropic API key: https://console.anthropic.com/

### 2. Install
```bash
git clone https://github.com/maiasalti/text-in-my-voice.git
cd text-in-my-voice
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Then give it your API key. Easiest is a local `.env` file (gitignored, and it
works both in the terminal and for the background service below):
```bash
cp .env.example .env
# open .env and replace sk-ant-your-real-key-here with your real key
```
Or just export it for the current shell: `export ANTHROPIC_API_KEY="sk-ant-..."`.

### 3. Build your voice
This reads your own sent messages and writes `voice/voice-profile.md` +
`voice/examples.md`:
```bash
python build_voice_profile.py            # extract + analyse with Claude
# or, no API analysis — just extract examples and edit the profile yourself:
python build_voice_profile.py --no-analyze
```
Useful flags: `--limit 20000` (scan more history), `--sample-size 200` (more
examples), `--out-dir voice` (where to write). You can always hand-edit the two
files afterwards — or copy `voice/*.template.md` and write them from scratch.

### 4. Run the watcher
```bash
python watch.py
```
Then text yourself from another device, or wait for a real message. It baselines
on the newest message at launch, so it won't spam drafts for your whole history.
Stop with **ctrl-c**.

---

## Running in the background

`python watch.py` runs in the foreground — closing the terminal stops it. To keep
it running without a terminal open:

**Quick (survives closing the tab, not logout):**
```bash
nohup python watch.py > watcher.log 2>&1 &
tail -f watcher.log     # watch it;  pkill -f watch.py  to stop
```

**Proper background service (starts at login, auto-restarts) — macOS LaunchAgent:**
```bash
cp launchd/com.imessage-reply-drafter.plist.example \
   ~/Library/LaunchAgents/com.imessage-reply-drafter.plist
# edit that file: replace /Users/YOUR_USERNAME/... with your real repo path
launchctl load -w ~/Library/LaunchAgents/com.imessage-reply-drafter.plist
```
- Put your key in `.env` (the agent can't read your shell profile).
- The plist sets `DRAFTER_NOTIFY=1`, so drafts arrive as macOS notifications
  (there's no live terminal feed when it runs this way); `watcher.log` has the full output.
- **Full Disk Access:** the agent runs `.venv/bin/python`, and *that binary* needs
  Full Disk Access to read `chat.db` — Terminal's permission doesn't transfer. If
  `watcher.log` shows `unable to open database file`, add the python binary in
  System Settings → Privacy & Security → Full Disk Access.
- Stop/remove it: `launchctl unload ~/Library/LaunchAgents/com.imessage-reply-drafter.plist`.

## Sticky-note window (optional)

`sticky.py` is an always-on-top yellow note that shows the latest drafted reply
and updates live. Click a suggestion to copy it, then ⌘V into Messages.

`watch.py` publishes each draft to `latest_draft.json`, which the sticky reads.

```bash
# Your Python needs a working Tk. The macOS command-line-tools Python ships a
# broken Tk 8.5, so install a good one and run the sticky with that interpreter:
brew install python-tk@3.13
/opt/homebrew/bin/python3.13 sticky.py
```

To keep it on screen permanently / at login, use the LaunchAgent template in
`launchd/com.imessage-reply-drafter-sticky.plist.example` (same install pattern
as the watcher). The sticky needs **no special permissions** — it only reads a
local file and writes to the clipboard.

## Configuration

All knobs live at the top of `watch.py`:

| Setting | Default | What it does |
|---|---|---|
| `POLL_INTERVAL` | `2.5` | Seconds between checks for new messages |
| `CONTEXT_MESSAGES` | `18` | How many recent thread messages to feed the model |
| `NUM_SUGGESTIONS` | `3` | How many reply options to draft |
| `MODEL` | `claude-opus-4-8` | Which Claude model to use |
| `EFFORT` | `low` | Thinking/spend level — `low` is fast and cheap |
| `COPY_TOP_TO_CLIPBOARD` | `False` | `pbcopy` the top suggestion (paste with ⌘V) |
| `SEND_NOTIFICATION` | `False` | Post a macOS notification when a draft is ready |
| `ALLOWLIST` | `[]` | If non-empty, only draft for matching people/chats |
| `BLOCKLIST` | `[]` | Never draft for matching people/chats (always wins) |
| `USE_DB_COPY` | `False` | DB access strategy (see below) |

`ALLOWLIST` / `BLOCKLIST` match (case-insensitive substring) against the sender's
handle (phone/email) and the chat name/identifier, e.g.
`ALLOWLIST = ["mum", "+15551234567", "alex@icloud.com"]`.

### Model choice & cost
It defaults to `claude-opus-4-8` (most capable). For a tool that fires on **every**
incoming text, you'll probably want something cheaper/faster — set
`MODEL = "claude-sonnet-4-6"` or `MODEL = "claude-haiku-4-5"`. The voice profile is
sent as a cached system prompt, so repeat calls cost less than they look.

---

## How it reads chat.db safely

Messages uses SQLite in **WAL mode**, and the newest messages sit in the
write-ahead log (`chat.db-wal`) before being folded into the main file.

- **`USE_DB_COPY = False` (default):** open `chat.db` **read-only and live**
  (`file:…?mode=ro`, autocommit). Read-only WAL readers don't block Messages, and
  each query runs in its own short read transaction so it sees just-arrived
  messages in the WAL. (This is deliberately *not* `immutable=1` — that flag tells
  SQLite to ignore the WAL, which would hide brand-new messages.)
- **`USE_DB_COPY = True`:** copy `chat.db` (+ `-wal`/`-shm`) to a temp dir each poll
  and read the copy. Maximum isolation; slower on a large DB.

Either way: **read-only, no writes, nothing sent.**

---

## Output

```
──────────────────────────────────────────────────────────────────────
[14:23:05]  📩  +15551234567  · Alex
     them: yo are we still on for tonight

     recent context:
       me: yeah lets do 8
       them: cool where

     suggestions:
       1) ya still on
          thinking that thai place near urs?
       2) yep !! 8 works
       3) ofc, u still keen for the thai spot
```

Newest at the bottom (a normal scrolling feed), so you keep scrollback. Flip
`COPY_TOP_TO_CLIPBOARD` / `SEND_NOTIFICATION` to `True` once the basic feed works.

---

## Limitations

- **`attributedBody` decoding:** newer macOS sometimes stores message text as an
  archived `NSAttributedString` (with `message.text` NULL). The tool decodes the
  common case heuristically; the rare undecodable message is just skipped.
- **Group chats:** supported — sender handles are shown in context so the model
  knows who said what.
- Only reacts to messages that arrive **after** it starts.

---

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Apple or Anthropic. Use it on
your own messages, at your own discretion, and mind the privacy note.
