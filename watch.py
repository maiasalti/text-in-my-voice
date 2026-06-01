#!/usr/bin/env python3
"""
imessage-reply-drafter — watches ~/Library/Messages/chat.db for new incoming
iMessages and drafts replies in Maia's voice via the Anthropic API.

READ-ONLY. Never writes to chat.db. Never sends anything. Drafts only.

See README.md for setup and the privacy note.
"""
from __future__ import annotations  # allow `str | None` / `list[str]` on Python 3.9

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import anthropic

# ── env bootstrap ────────────────────────────────────────────────────────────
# Load a local .env file so your API key works both in the terminal AND when run
# as a background LaunchAgent (which does NOT read your shell profile / ~/.zshrc).
SCRIPT_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:   # real env vars still win
            os.environ[key] = val


_load_dotenv(SCRIPT_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — tweak these
# ─────────────────────────────────────────────────────────────────────────────

POLL_INTERVAL = 2.5            # seconds between checks for new messages
CONTEXT_MESSAGES = 18          # how many recent messages of the thread to feed the model
NUM_SUGGESTIONS = 3            # how many reply options to draft

MODEL = "claude-opus-4-8"      # see README — Haiku/Sonnet are cheaper+faster for this
EFFORT = "low"                 # low = fast & cheap; fine for short casual replies

COPY_TOP_TO_CLIPBOARD = _env_bool("DRAFTER_CLIPBOARD", False)  # ⌘V the top option; or set DRAFTER_CLIPBOARD=1
SEND_NOTIFICATION = _env_bool("DRAFTER_NOTIFY", False)         # macOS notification; or set DRAFTER_NOTIFY=1

# Contact filtering. Matching is case-insensitive substring against the sender's
# handle (phone/email) AND the chat name/identifier.
#   - If ALLOWLIST is non-empty, ONLY draft for chats/people that match it.
#   - BLOCKLIST always wins: never draft for anything that matches it.
# Examples: ALLOWLIST = ["+15551234567", "mum", "alex@icloud.com"]
ALLOWLIST: list[str] = []
BLOCKLIST: list[str] = []

# DB access mode:
#   False (default) = open chat.db live, read-only (sees brand-new messages in the WAL).
#   True            = copy chat.db (+wal/+shm) to a temp dir each poll, then read the copy.
#                     Safer/most isolated, but copies the whole DB every cycle (slow on big DBs).
USE_DB_COPY = False

# ─────────────────────────────────────────────────────────────────────────────

HOME = Path.home()
DB_PATH = HOME / "Library" / "Messages" / "chat.db"
# Your voice lives in ./voice/ — generate it with build_voice_profile.py, or
# copy the .template.md files and edit them. Override the dir with $VOICE_DIR.
VOICE_DIR = Path(os.environ.get("VOICE_DIR", SCRIPT_DIR / "voice"))
VOICE_PROFILE = VOICE_DIR / "voice-profile.md"
VOICE_EXAMPLES = VOICE_DIR / "examples.md"
# The latest draft is published here for the sticky-note window (sticky.py).
LATEST_DRAFT_PATH = SCRIPT_DIR / "latest_draft.json"

# Apple absolute time (nanoseconds since 2001-01-01) -> unix epoch seconds.
APPLE_EPOCH = 978307200

# Structured output schema — guarantees clean, parseable drafts.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "should_reply": {"type": "boolean"},
        "reason": {
            "type": "string",
            "description": "If should_reply is false, a short reason why no reply is needed.",
        },
        "suggestions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Reply options in Maia's voice. Each item is one full reply, which may itself "
                "be several short texts separated by newlines (she often fires off a few in a row)."
            ),
        },
    },
    "required": ["should_reply", "reason", "suggestions"],
    "additionalProperties": False,
}


# ─── voice + prompt ──────────────────────────────────────────────────────────

def build_system_prompt() -> str:
    if not VOICE_PROFILE.exists() or not VOICE_EXAMPLES.exists():
        sys.exit(
            f"No voice profile found in {VOICE_DIR}/\n\n"
            "Build one from your own sent texts:\n"
            "    python build_voice_profile.py\n\n"
            "…or copy the templates and edit them by hand:\n"
            f"    cp {VOICE_DIR}/voice-profile.template.md {VOICE_PROFILE}\n"
            f"    cp {VOICE_DIR}/examples.template.md {VOICE_EXAMPLES}"
        )
    profile = VOICE_PROFILE.read_text(encoding="utf-8")
    examples = VOICE_EXAMPLES.read_text(encoding="utf-8")
    return f"""You draft text-message replies in Maia's own voice. Your job is fidelity to how \
she actually texts — NOT polished, well-formed writing. The single biggest failure mode is \
over-polishing: do not clean up her lowercase, do not add ending periods, do not swap her \
slang for full words.

# Maia's voice profile
{profile}

# Real examples of how she texts (verbatim)
{examples}

# Your task
You'll be given a conversation thread and the new incoming message she just received. Draft \
reply options that sound exactly like the examples above.

Rules:
- Match her register to the situation (close friend / neutral-warm / affection / serious-family).
  When unsure, default to neutral-warm.
- Keep each option SHORT. If a thought has multiple beats, split it into several short texts \
  on separate lines rather than one long message.
- lowercase by default, minimal punctuation, no ending periods, her slang (u, ur, gonna, lmk, \
  dw, ngl, etc.), her endearments (darl/darling/dude/legend — never "bestie").
- Don't force warmth, emoji, or exclamation marks she wouldn't use. Shorter is righter.
- If the incoming message clearly doesn't need a reply (e.g. she already had the last word, \
  it's spam, or it's a reaction/acknowledgement), set should_reply to false and say why in \
  reason — don't force a reply.

Return your answer only through the structured output format."""


# ─── chat.db reading ─────────────────────────────────────────────────────────

def decode_attributed_body(blob: bytes) -> str | None:
    """Best-effort text extraction from message.attributedBody.

    Modern macOS often stores the message text as an NSAttributedString
    (typedstream archive) in attributedBody, leaving message.text NULL.
    This is a heuristic decode that handles the common case; it's not a full
    typedstream parser. Returns None if it can't find text.
    """
    if not blob:
        return None
    try:
        if b"NSString" not in blob:
            return None
        chunk = blob.split(b"NSString", 1)[1]
        chunk = chunk[5:]  # skip the class marker bytes (commonly 01 94 84 01 2b)
        if not chunk:
            return None
        if chunk[0] == 0x81:  # long string: next 2 bytes are little-endian length
            length = int.from_bytes(chunk[1:3], "little")
            start = 3
        else:
            length = chunk[0]
            start = 1
        text = chunk[start:start + length].decode("utf-8", "replace").strip()
        return text or None
    except Exception:
        return None


def message_text(text, attributed_body) -> str | None:
    if text:
        return text
    return decode_attributed_body(attributed_body)


def open_live_connection() -> sqlite3.Connection:
    """Persistent read-only connection. Autocommit so each SELECT sees the
    latest committed data (including the WAL), with no write transaction held."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def copy_db_to_temp(tmp: Path) -> Path:
    """Copy chat.db (+wal/+shm) into tmp and return the copied db path."""
    dest = tmp / "chat.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(DB_PATH) + suffix)
        if src.exists():
            shutil.copy2(src, str(dest) + suffix)
    return dest


def query_new_messages(conn: sqlite3.Connection, last_rowid: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.ROWID            AS rowid,
               m.text             AS text,
               m.attributedBody   AS attributed_body,
               m.is_from_me       AS is_from_me,
               m.date             AS date,
               h.id               AS handle,
               cmj.chat_id        AS chat_id,
               c.chat_identifier  AS chat_identifier,
               c.display_name     AS display_name,
               c.style            AS chat_style
        FROM message m
        LEFT JOIN handle h            ON m.handle_id = h.ROWID
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c              ON c.ROWID = cmj.chat_id
        WHERE m.ROWID > ?
        ORDER BY m.ROWID ASC
        """,
        (last_rowid,),
    ).fetchall()


def query_thread_context(conn: sqlite3.Connection, chat_id: int, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT m.text           AS text,
               m.attributedBody AS attributed_body,
               m.is_from_me     AS is_from_me,
               h.id             AS handle
        FROM message m
        LEFT JOIN handle h            ON m.handle_id = h.ROWID
        JOIN chat_message_join cmj    ON cmj.message_id = m.ROWID
        WHERE cmj.chat_id = ?
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (chat_id, limit),
    ).fetchall()
    out = []
    for r in reversed(rows):  # back to chronological order
        body = message_text(r["text"], r["attributed_body"])
        if not body:
            continue
        out.append({
            "from_me": bool(r["is_from_me"]),
            "handle": r["handle"],
            "text": body,
        })
    return out


def current_max_rowid(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(ROWID) AS m FROM message").fetchone()
    return row["m"] or 0


# ─── filtering / formatting ──────────────────────────────────────────────────

def sender_label(row) -> str:
    return row["handle"] or "unknown"


def chat_label(row) -> str:
    return row["display_name"] or row["chat_identifier"] or "(unknown chat)"


def is_group(row) -> bool:
    # chat.style 43 = group, 45 = 1:1 on most macOS versions
    return row["chat_style"] == 43


def passes_filters(row) -> bool:
    haystack = " ".join(
        str(x).lower()
        for x in (row["handle"], row["chat_identifier"], row["display_name"])
        if x
    )
    for term in BLOCKLIST:
        if term.lower() in haystack:
            return False
    if ALLOWLIST:
        return any(term.lower() in haystack for term in ALLOWLIST)
    return True


def format_thread_for_model(context: list[dict], group: bool) -> str:
    lines = []
    for msg in context:
        if msg["from_me"]:
            who = "me"
        elif group:
            who = msg["handle"] or "them"
        else:
            who = "them"
        lines.append(f"{who}: {msg['text']}")
    return "\n".join(lines) if lines else "(no earlier messages)"


# ─── drafting ────────────────────────────────────────────────────────────────

def draft_replies(client, system_prompt, row, context) -> dict:
    group = is_group(row)
    incoming = message_text(row["text"], row["attributed_body"]) or ""
    thread = format_thread_for_model(context, group)
    sender = sender_label(row)

    user_content = f"""Conversation thread so far (oldest first, most recent last):
{thread}

NEW incoming message from {sender}: "{incoming}"
Chat: {chat_label(row)} ({'group chat' if group else '1:1'})

Draft {NUM_SUGGESTIONS} reply options in Maia's voice."""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},  # voice profile is stable -> cache it
        }],
        messages=[{"role": "user", "content": user_content}],
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


# ─── output ──────────────────────────────────────────────────────────────────

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def show(row, context, result):
    ts = datetime.now().strftime("%H:%M:%S")
    sender = sender_label(row)
    incoming = message_text(row["text"], row["attributed_body"]) or "(no text)"
    group = is_group(row)

    print(f"\n{DIM}{'─' * 70}{RESET}")
    print(f"{DIM}[{ts}]{RESET}  📩  {BOLD}{sender}{RESET}  {DIM}· {chat_label(row)}{RESET}")
    print(f"     {CYAN}them:{RESET} {incoming}")

    if context and len(context) > 1:
        print(f"\n     {DIM}recent context:{RESET}")
        for msg in context[-6:-1]:  # a few lines before the new one
            who = "me" if msg["from_me"] else (msg["handle"] if group else "them")
            print(f"       {DIM}{who}: {msg['text']}{RESET}")

    if not result.get("should_reply", True):
        reason = result.get("reason", "")
        print(f"\n     {YELLOW}↳ probably no reply needed{RESET} {DIM}— {reason}{RESET}")
        return

    print(f"\n     {GREEN}suggestions:{RESET}")
    for i, sug in enumerate(result.get("suggestions", []), 1):
        parts = sug.split("\n")
        print(f"       {BOLD}{i}){RESET} {parts[0]}")
        for cont in parts[1:]:
            print(f"          {cont}")


def write_latest_draft(row, result):
    """Publish the latest draft to a JSON file for the sticky-note window."""
    try:
        data = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "sender": sender_label(row),
            "chat": chat_label(row),
            "incoming": message_text(row["text"], row["attributed_body"]) or "",
            "should_reply": bool(result.get("should_reply", True)),
            "reason": result.get("reason", ""),
            "suggestions": result.get("suggestions", []),
        }
        LATEST_DRAFT_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def copy_to_clipboard(text: str):
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    except Exception as e:
        print(f"{DIM}(clipboard copy failed: {e}){RESET}")


def notify(title: str, body: str):
    safe = lambda s: s.replace('"', '\\"')
    script = f'display notification "{safe(body)}" with title "{safe(title)}"'
    try:
        subprocess.run(["osascript", "-e", script], check=True)
    except Exception:
        pass


# ─── main loop ───────────────────────────────────────────────────────────────

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. export it first (see README).")
    if not DB_PATH.exists():
        sys.exit(f"chat.db not found at {DB_PATH}. Is Messages set up?")

    system_prompt = build_system_prompt()
    client = anthropic.Anthropic()

    print(f"{BOLD}imessage-reply-drafter{RESET}")
    print(f"  model: {MODEL} (effort={EFFORT})   poll: {POLL_INTERVAL}s   "
          f"context: {CONTEXT_MESSAGES}   options: {NUM_SUGGESTIONS}")
    print(f"  db mode: {'copy-per-poll' if USE_DB_COPY else 'live read-only'}")
    if ALLOWLIST:
        print(f"  allowlist: {ALLOWLIST}")
    if BLOCKLIST:
        print(f"  blocklist: {BLOCKLIST}")
    print(f"  {DIM}drafts only — nothing is ever sent. read-only on chat.db.{RESET}")

    # Establish baseline so we only react to genuinely new messages.
    tmpdir = None
    if USE_DB_COPY:
        tmpdir = Path(tempfile.mkdtemp(prefix="imsg-drafter-"))
        conn = sqlite3.connect(f"file:{copy_db_to_temp(tmpdir)}?mode=ro", uri=True,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
    else:
        conn = open_live_connection()

    last_rowid = current_max_rowid(conn)
    if USE_DB_COPY:
        conn.close()
    print(f"  watching for new messages (baseline rowid={last_rowid})… ctrl-c to stop\n")

    try:
        while True:
            time.sleep(POLL_INTERVAL)
            try:
                if USE_DB_COPY:
                    db = copy_db_to_temp(tmpdir)
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, isolation_level=None)
                    conn.row_factory = sqlite3.Row

                rows = query_new_messages(conn, last_rowid)
                for row in rows:
                    last_rowid = max(last_rowid, row["rowid"])
                    if row["is_from_me"]:
                        continue                       # only react to received messages
                    if row["chat_id"] is None:
                        continue
                    if not passes_filters(row):
                        continue
                    incoming = message_text(row["text"], row["attributed_body"])
                    if not incoming:
                        continue                       # attachment-only / undecodable

                    context = query_thread_context(conn, row["chat_id"], CONTEXT_MESSAGES)
                    try:
                        result = draft_replies(client, system_prompt, row, context)
                    except Exception as e:
                        print(f"{YELLOW}draft failed for {sender_label(row)}: {e}{RESET}")
                        continue

                    show(row, context, result)
                    write_latest_draft(row, result)

                    if result.get("should_reply", True) and result.get("suggestions"):
                        top = result["suggestions"][0]
                        if COPY_TOP_TO_CLIPBOARD:
                            copy_to_clipboard(top)
                            print(f"     {DIM}↳ top option copied to clipboard{RESET}")
                        if SEND_NOTIFICATION:
                            notify(f"Reply to {sender_label(row)}", top.replace("\n", " "))
            finally:
                if USE_DB_COPY:
                    conn.close()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if not USE_DB_COPY:
            conn.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
