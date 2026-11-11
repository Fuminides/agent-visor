"""The dialog through which ssh asks the user when `visor follow` needs a password, a code or a yes.
ssh runs this as its SSH_ASKPASS program, with its question as the argument, and reads the answer from the
output; nothing is kept. SSH_ASKPASS_PROMPT tells a yes/no question ("confirm") and a notice ("none") apart."""
from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import font as tkfont

from . import i18n
from .app import SERIFS, THEMES
from .mirror import ASKED
from .storage import Store


class Dialog:
    def __init__(self, root, prompt, kind=None, language="en", theme="sand"):
        self.root, self.answer = root, None
        colors = THEMES.get(theme, THEMES["sand"])
        families = set(tkfont.families(root))
        sans = "Inter" if "Inter" in families else "DejaVu Sans"
        serif = next((name for name in SERIFS if name in families), sans)
        # A new host's key is confirmed by typing yes; a button says it better.
        question = kind == "confirm" or "yes/no" in prompt
        root.title("Visor")
        root.configure(bg=colors["BG"], padx=26, pady=22)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.cancel)
        tk.Label(root, text=i18n.text(language, "ask_title"), bg=colors["BG"], fg=colors["INK"], anchor="w",
                 font=(serif, 16) if serif != sans else (sans, 14, "bold")).pack(fill="x")
        tk.Label(root, text=prompt.strip(), bg=colors["BG"], fg=colors["INK"], font=(sans, 10), anchor="w", justify="left",
                 wraplength=400).pack(fill="x", pady=(10, 12))
        self.entry = None
        if not question and kind != "none":
            self.entry = tk.Entry(root, show="•", width=38, bg=colors["FIELD"], fg=colors["INK"], insertbackground=colors["INK"],
                                  relief="flat", font=(sans, 11), highlightthickness=2, highlightbackground=colors["EDGE"],
                                  highlightcolor=colors["ACCENT"])
            self.entry.pack(fill="x", ipady=6)
            tk.Label(root, text=i18n.text(language, "ask_hint"), bg=colors["BG"], fg=colors["MUTED"], font=(sans, 8),
                     anchor="w").pack(fill="x", pady=(8, 0))
        row = tk.Frame(root, bg=colors["BG"])
        row.pack(fill="x", pady=(18, 0))

        def button(label, command, primary):
            fill, hot, ink = ((colors["ACCENT"], colors["ACCENT_HOT"], colors["ON_ACCENT"]) if primary else
                              (colors["CARD"], colors["PRESSED"], colors["INK"]))
            return tk.Button(row, text=i18n.text(language, label), command=command, bg=fill, fg=ink, activebackground=hot,
                             activeforeground=ink, relief="flat", bd=0, padx=16, pady=7, cursor="hand2", font=(sans, 10),
                             highlightthickness=2, highlightbackground=colors["BG"], highlightcolor=colors["INK"])
        self.accept_button = button("ask_ok" if kind == "none" else "ask_yes" if question else "ask_connect", self.accept, True)
        self.accept_button.pack(side="right")
        if kind != "none":
            button("ask_no" if question else "ask_cancel", self.cancel, False).pack(side="right", padx=(0, 8))
        self.typed = "yes" if question and kind != "confirm" else ""
        root.bind("<Return>", lambda event: self.accept())
        root.bind("<Escape>", lambda event: self.cancel())
        root.update_idletasks()
        root.geometry(f"+{(root.winfo_screenwidth() - root.winfo_reqwidth()) // 2}+{(root.winfo_screenheight() - root.winfo_reqheight()) // 3}")
        root.attributes("-topmost", True)
        (self.entry or self.accept_button).focus_force()

    def accept(self):
        self.answer = self.entry.get() if self.entry else self.typed
        self.root.quit()

    def cancel(self):
        self.root.quit()


def main(prompt):
    print(ASKED, file=sys.stderr, flush=True)
    store = Store()
    root = tk.Tk(className="AgentVisor")
    dialog = Dialog(root, prompt, os.environ.get("SSH_ASKPASS_PROMPT"), store.setting("language", "en"), store.setting("theme", "sand"))
    asker = os.getppid()

    def watch():  # ssh gave up (the other computer stopped waiting): nobody is left to answer
        if os.getppid() != asker:
            dialog.cancel()
        root.after(1000, watch)
    watch()
    root.mainloop()
    if dialog.answer is None:
        return 1
    print(dialog.answer)
    return 0
