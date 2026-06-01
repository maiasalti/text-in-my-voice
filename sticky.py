#!/usr/bin/env python3
"""
sticky.py — an always-on-top "sticky note" that shows the latest drafted reply.

Reads latest_draft.json (written by watch.py) and updates live. Click a
suggestion to copy it to the clipboard, then ⌘V into Messages.

Run it alongside watch.py:  python sticky.py
(or install the sticky LaunchAgent so it's always on screen — see README).
"""
from __future__ import annotations

import json
import subprocess
import tkinter as tk
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DRAFT_FILE = SCRIPT_DIR / "latest_draft.json"

BG = "#FFF6B8"      # sticky-note yellow
LINE = "#E8DA7A"
POLL_MS = 1000


def pbcopy(text: str) -> None:
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    except Exception:
        pass


class Sticky:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("iMessage drafts")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.geometry("370x440+60+80")
        self.root.minsize(300, 200)
        self.note = None
        self._mtime = -1.0
        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=14, pady=14)
        self._render_waiting()
        self._poll()

    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.note = None

    def _render_waiting(self):
        self._clear()
        tk.Label(self.body, text="📝 drafts will appear here", bg=BG, fg="#7a6b00",
                 font=("Helvetica", 14, "bold")).pack(anchor="w")
        tk.Label(self.body, text="waiting for your next message…", bg=BG, fg="#998400",
                 font=("Helvetica", 12)).pack(anchor="w", pady=(4, 0))

    def _render(self, d: dict):
        self._clear()
        sender = d.get("sender") or "someone"
        tk.Label(self.body, text=f"📩  {sender}", bg=BG, fg="#5a4f00",
                 font=("Helvetica", 13, "bold")).pack(anchor="w")
        chat = d.get("chat")
        if chat and chat != sender:
            tk.Label(self.body, text=chat, bg=BG, fg="#8a7c00",
                     font=("Helvetica", 10)).pack(anchor="w")
        tk.Label(self.body, text=d.get("incoming", ""), bg=BG, fg="#1d1d1d",
                 wraplength=330, justify="left",
                 font=("Helvetica", 13)).pack(anchor="w", pady=(5, 9))
        tk.Frame(self.body, bg=LINE, height=1).pack(fill="x", pady=(0, 9))

        if not d.get("should_reply", True):
            tk.Label(self.body, text="↳ probably no reply needed", bg=BG, fg="#996f00",
                     font=("Helvetica", 12, "italic"),
                     wraplength=330, justify="left").pack(anchor="w")
            if d.get("reason"):
                tk.Label(self.body, text=d["reason"], bg=BG, fg="#8a7c00",
                         wraplength=330, justify="left",
                         font=("Helvetica", 10)).pack(anchor="w", pady=(3, 0))
            return

        tk.Label(self.body, text="tap one to copy:", bg=BG, fg="#7a6b00",
                 font=("Helvetica", 10)).pack(anchor="w", pady=(0, 4))
        for i, s in enumerate(d.get("suggestions", []), 1):
            tk.Button(
                self.body, text=f"{i})  {s}", bg="white", fg="#1d1d1d",
                wraplength=300, justify="left", anchor="w", relief="flat", bd=0,
                padx=11, pady=9, font=("Helvetica", 12), activebackground=LINE,
                highlightthickness=0, command=lambda t=s: self._copy(t),
            ).pack(fill="x", pady=3)
        self.note = tk.Label(self.body, text="", bg=BG, fg="#2a7a00", wraplength=330,
                             justify="left", font=("Helvetica", 11, "bold"))
        self.note.pack(anchor="w", pady=(8, 0))

    def _copy(self, text: str):
        pbcopy(text)
        if self.note is not None:
            self.note.config(text="✓ copied — click the Messages box and ⌘V")

    def _poll(self):
        try:
            if DRAFT_FILE.exists():
                m = DRAFT_FILE.stat().st_mtime
                if m != self._mtime:
                    self._mtime = m
                    self._render(json.loads(DRAFT_FILE.read_text(encoding="utf-8")))
                    self.root.attributes("-topmost", True)
                    self.root.lift()
        except Exception:
            pass
        self.root.after(POLL_MS, self._poll)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Sticky().run()
