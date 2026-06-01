#!/usr/bin/env python3
"""
build_voice_profile.py — turn YOUR own sent iMessages into a voice profile the
watcher can draft in.

Reads ~/Library/Messages/chat.db READ-ONLY and writes two files into ./voice/:
  - examples.md       a sample of your real sent messages (texture reference)
  - voice-profile.md  a written description of how you text

By default it also analyses a sample with Claude to write voice-profile.md
(needs ANTHROPIC_API_KEY). Use --no-analyze to skip that — it'll still write
examples.md and drop a template voice-profile.md for you to edit by hand.

Needs Full Disk Access for your terminal app. Nothing leaves your machine unless
you let it analyse — and then it only sends a sample of YOUR OWN sent messages,
with your own API key.

Usage:
    python build_voice_profile.py                 # extract + analyse
    python build_voice_profile.py --no-analyze    # extract only
    python build_voice_profile.py --limit 20000   # scan more history
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import statistics
import sys
from pathlib import Path

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = HOME / "Library" / "Messages" / "chat.db"

MODEL = "claude-opus-4-8"  # used only for --analyze

# Rough emoji detector (good enough for a stat, not exhaustive).
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"   # most emoji + supplemental symbols
    "\U00002600-\U000027BF"    # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"    # regional indicators (flags)
    "\U00002190-\U000021FF"    # arrows
    "\U00002300-\U000023FF]"   # misc technical (⌚ ⏰ etc.)
)


def decode_attributed_body(blob):
    """Best-effort text extraction from message.attributedBody (typedstream)."""
    if not blob:
        return None
    try:
        if b"NSString" not in blob:
            return None
        chunk = blob.split(b"NSString", 1)[1][5:]
        if not chunk:
            return None
        if chunk[0] == 0x81:
            length = int.from_bytes(chunk[1:3], "little")
            start = 3
        else:
            length = chunk[0]
            start = 1
        text = chunk[start:start + length].decode("utf-8", "replace").strip()
        return text or None
    except Exception:
        return None


def text_of(text, attributed_body):
    return text if text else decode_attributed_body(attributed_body)


def fetch_sent(limit: int) -> list[str]:
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT text, attributedBody AS ab FROM message
           WHERE is_from_me = 1
           ORDER BY date DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        t = text_of(r["text"], r["ab"])
        if t and t.strip():
            out.append(t.strip())
    return out


def compute_stats(msgs: list[str]) -> dict:
    n = len(msgs)
    words = [len(m.split()) for m in msgs]

    def first_alpha_is_lower(m):
        for ch in m:
            if ch.isalpha():
                return ch.islower()
        return None

    lower_flags = [x for x in (first_alpha_is_lower(m) for m in msgs) if x is not None]
    no_end = sum(1 for m in msgs if m[-1] not in ".!?")
    emoji = sum(1 for m in msgs if EMOJI_RE.search(m))
    return {
        "count": n,
        "median_words": int(statistics.median(words)) if words else 0,
        "pct_start_lower": round(100 * sum(lower_flags) / len(lower_flags)) if lower_flags else 0,
        "pct_no_end_punct": round(100 * no_end / n) if n else 0,
        "pct_emoji": round(100 * emoji / n, 1) if n else 0,
    }


def even_sample(msgs: list[str], k: int) -> list[str]:
    """Dedupe (case-insensitive) and take an even spread across history."""
    seen, uniq = set(), []
    for m in msgs:
        key = m.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(m)
    if len(uniq) <= k:
        return uniq
    step = len(uniq) / k
    return [uniq[int(i * step)] for i in range(k)]


def write_examples(path: Path, sample: list[str], stats: dict):
    header = [
        "# Examples — real messages I've sent\n",
        "Verbatim samples of my own texts, used to calibrate the drafting voice. "
        "Don't copy them literally — they set the *feel* (spelling, capitalization, "
        "punctuation, length, emoji).\n",
        f"_Sampled from {stats['count']} sent messages · "
        f"median ~{stats['median_words']} words · "
        f"{stats['pct_start_lower']}% start lowercase · "
        f"{stats['pct_no_end_punct']}% no ending punctuation · "
        f"{stats['pct_emoji']}% contain an emoji._\n",
    ]
    body = [f"- {m.replace(chr(10), ' / ')}" for m in sample]
    path.write_text("\n".join(header + body) + "\n", encoding="utf-8")


def analyze(sample: list[str], stats: dict) -> str:
    import anthropic

    client = anthropic.Anthropic()
    joined = "\n".join(f"- {m}" for m in sample)
    prompt = f"""Below are real text messages that ONE person sent (verbatim). \
Summary stats across their history: median ~{stats['median_words']} words per text, \
{stats['pct_start_lower']}% start with a lowercase letter, {stats['pct_no_end_punct']}% \
have no ending punctuation, {stats['pct_emoji']}% contain an emoji.

Messages:
{joined}

Write a `voice-profile.md` that describes exactly how this person texts, so that an \
AI could later draft new messages indistinguishable from theirs. Be specific and cite \
the patterns you see (capitalization, punctuation, length & rhythm, slang/shorthand, \
recurring phrases and endearments, emoji/emoticon habits, and how their tone shifts by \
who they're talking to or how serious the moment is). Describe their *real* habits even \
where they break "good writing" rules. Use these section headers:

# Voice profile — how I text
## Capitalization
## Punctuation
## Length & rhythm
## Slang, fillers, recurring phrases
## Emoji & emoticons
## Tone & register

Output only the markdown, no preamble."""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=6000,
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "").strip()


def main():
    ap = argparse.ArgumentParser(description="Build a texting voice profile from your sent iMessages.")
    ap.add_argument("--limit", type=int, default=8000,
                    help="how many recent sent messages to scan (default 8000)")
    ap.add_argument("--out-dir", default=str(SCRIPT_DIR / "voice"),
                    help="where to write voice-profile.md and examples.md")
    ap.add_argument("--sample-size", type=int, default=150,
                    help="how many messages to write into examples.md (default 150)")
    ap.add_argument("--no-analyze", action="store_true",
                    help="skip the Claude analysis step (just extract examples + stats)")
    args = ap.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"chat.db not found at {DB_PATH}. Is Messages set up on this Mac?")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"reading sent messages from {DB_PATH} (read-only)…")
    msgs = fetch_sent(args.limit)
    if not msgs:
        sys.exit("No sent messages found. Check Full Disk Access for your terminal app.")

    stats = compute_stats(msgs)
    print(f"  found {stats['count']} sent messages "
          f"(~{stats['median_words']} median words, "
          f"{stats['pct_start_lower']}% lowercase-start, "
          f"{stats['pct_no_end_punct']}% no end punctuation, "
          f"{stats['pct_emoji']}% emoji)")

    examples_path = out / "examples.md"
    write_examples(examples_path, even_sample(msgs, args.sample_size), stats)
    print(f"  wrote {examples_path}")

    profile_path = out / "voice-profile.md"
    if args.no_analyze or not os.environ.get("ANTHROPIC_API_KEY"):
        if not args.no_analyze:
            print("  ANTHROPIC_API_KEY not set — skipping Claude analysis.")
        template = out / "voice-profile.template.md"
        if not profile_path.exists() and template.exists():
            profile_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  wrote {profile_path} from the template — edit it to describe your voice.")
        print("\nDone. Set ANTHROPIC_API_KEY and re-run for an auto-written profile, "
              "or edit voice-profile.md by hand. Then run:  python watch.py")
        return

    print("  analysing your style with Claude…")
    profile = analyze(even_sample(msgs, 500), stats)
    if not profile:
        sys.exit("  analysis returned nothing — try again, or use --no-analyze and edit by hand.")
    profile_path.write_text(profile + "\n", encoding="utf-8")
    print(f"  wrote {profile_path}")
    print("\nDone — now run:  python watch.py")


if __name__ == "__main__":
    main()
