#!/usr/bin/env bash
# One-shot installer for text-in-my-voice.
# Safe to re-run — it skips whatever's already done.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }

bold "text-in-my-voice — setup"

# ── 1. virtualenv + dependencies ─────────────────────────────────────────────
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1 || true
.venv/bin/python -m pip install -q -r requirements.txt
ok "Python environment ready (.venv)"

# ── 2. .env + API key ────────────────────────────────────────────────────────
[ -f .env ] || cp .env.example .env
if grep -q '^ANTHROPIC_API_KEY=sk-ant-' .env && ! grep -q 'sk-ant-your-real-key-here' .env; then
  ok "API key already set in .env"
else
  echo
  echo "  Paste your Anthropic API key (from https://console.anthropic.com/ → API Keys)."
  echo "  Input is hidden; it's written only to your local .env (gitignored)."
  printf "  Key: "
  read -rs KEY; echo
  if [ -n "${KEY:-}" ]; then
    tmp="$(mktemp)"
    grep -v '^ANTHROPIC_API_KEY=' .env > "$tmp" || true
    echo "ANTHROPIC_API_KEY=$KEY" >> "$tmp"
    mv "$tmp" .env
    ok "Saved API key to .env"
  else
    warn "No key entered — add it to .env later, then re-run ./setup.sh"
  fi
fi

# ── 3. GUI-capable Python for the sticky note (optional) ─────────────────────
STICKY_PY=""
find_tk_py() {
  for p in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
           /usr/local/bin/python3.13 /usr/local/bin/python3.12; do
    if [ -x "$p" ] && "$p" -c "import tkinter" >/dev/null 2>&1; then STICKY_PY="$p"; return; fi
  done
}
find_tk_py
if [ -z "$STICKY_PY" ] && command -v brew >/dev/null 2>&1; then
  echo "  Installing a GUI-capable Python for the sticky note (brew)…"
  brew install python-tk@3.13 >/dev/null 2>&1 || true
  find_tk_py
fi
if [ -n "$STICKY_PY" ]; then ok "Sticky-note Python: $STICKY_PY"
else warn "No Tk-capable Python found — skipping the sticky note (the watcher still works)."; fi

# ── 4. Full Disk Access check ────────────────────────────────────────────────
FDA_OK=0
if .venv/bin/python - <<PY >/dev/null 2>&1
import sqlite3, os
db = os.path.expanduser("~/Library/Messages/chat.db")
sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute("SELECT 1 FROM message LIMIT 1")
PY
then FDA_OK=1; ok "Full Disk Access OK (can read chat.db)"
else warn "Full Disk Access NOT granted yet — see the steps printed at the end."; fi

# ── 5. build the voice profile (needs key + FDA) ─────────────────────────────
KEY_SET=0
grep -q '^ANTHROPIC_API_KEY=sk-ant-' .env && ! grep -q 'sk-ant-your-real-key-here' .env && KEY_SET=1
if [ ! -f voice/voice-profile.md ] || grep -q "This is a TEMPLATE" voice/voice-profile.md 2>/dev/null; then
  if [ "$FDA_OK" = 1 ] && [ "$KEY_SET" = 1 ]; then
    echo "  Building your voice profile from your sent messages…"
    .venv/bin/python build_voice_profile.py || warn "voice build failed — run it manually later"
  else
    warn "Voice profile not built yet (needs API key + Full Disk Access). Re-run ./setup.sh after granting them."
  fi
else
  ok "Voice profile already present"
fi

# ── 6. install + (re)load the LaunchAgents ───────────────────────────────────
cat > "$LA/com.imessage-reply-drafter.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.imessage-reply-drafter</string>
  <key>ProgramArguments</key><array>
    <string>$REPO/.venv/bin/python</string><string>$REPO/watch.py</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict>
    <key>DRAFTER_NOTIFY</key><string>1</string>
    <key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$REPO/watcher.log</string>
  <key>StandardErrorPath</key><string>$REPO/watcher.log</string>
</dict></plist>
PLIST
launchctl unload "$LA/com.imessage-reply-drafter.plist" 2>/dev/null || true
launchctl load -w "$LA/com.imessage-reply-drafter.plist"
ok "Watcher service installed and started"

if [ -n "$STICKY_PY" ]; then
  cat > "$LA/com.imessage-reply-drafter-sticky.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.imessage-reply-drafter-sticky</string>
  <key>ProgramArguments</key><array>
    <string>$STICKY_PY</string><string>$REPO/sticky.py</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$REPO/sticky.log</string>
  <key>StandardErrorPath</key><string>$REPO/sticky.log</string>
</dict></plist>
PLIST
  launchctl unload "$LA/com.imessage-reply-drafter-sticky.plist" 2>/dev/null || true
  launchctl load -w "$LA/com.imessage-reply-drafter-sticky.plist"
  ok "Sticky-note service installed and started"
fi

# ── 7. what's left for the human ─────────────────────────────────────────────
echo
bold "Done."
if [ "$FDA_OK" != 1 ]; then
  echo
  bold "ACTION NEEDED — grant Full Disk Access:"
  echo "  1. The Settings pane will open. Click + and add this binary (⌘⇧G to paste the path):"
  echo "       $REPO/.venv/bin/python"
  echo "     (If macOS won't add it, add the Python.app it points to — see README.)"
  echo "  2. Toggle it ON, then re-run:  ./setup.sh"
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || true
else
  echo "Everything's running in the background (starts at login)."
  echo "  • Notifications appear on new messages; the yellow sticky shows drafts."
  echo "  • Watch it:  tail -f $REPO/watcher.log"
  echo "  • Stop:      launchctl unload $LA/com.imessage-reply-drafter.plist"
fi
