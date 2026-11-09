"""Native Tk window. All widget operations stay on the Tk event thread."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import shlex
import socketserver
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
import traceback

from PIL import Image, ImageDraw, ImageTk

from . import chat, i18n
from .ipc import MAX_REQUEST, runtime_dir, socket_path
from .storage import Store, signature

BG = "#101c22"
PANEL = "#17262d"
CARD = "#20323a"
SELECTED = "#294b50"
INK = "#e8f0ed"
MUTED = "#91a7aa"
ACCENT = "#87d8bc"
PAPER = "#f3f5ef"
DARK = "#203630"
ERROR = "#ff9b8a"
PERIODS = ("all", "today", "hour")
# Text greys chosen for at least 4.5:1 contrast on their backgrounds.
SOFT = "#607268"
FAINT = "#647167"
# With the question panel open the window needs room for sidebar, panel and a usable figure.
CHAT_MIN_WINDOW = 1080
INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)")


def entry(item):
    """History entry an item belongs to: its deck, or the image itself."""
    return item.get("deck") or item["id"]


class Visor:
    def __init__(self, root, store):
        self.root = root
        self.store = store
        self.events = queue.Queue()
        self.session = None
        self.items = []
        self.current = None
        self.pages = []
        self.original = None
        self.photo = None
        self.thumbnails = []
        self.fit = True
        self.scale = 1.0
        self.pan = [0, 0]
        self.pending_draw = None
        self.watch_busy = False
        self.watch_candidate = None
        self.follow = tk.BooleanVar(value=store.setting("follow", True))
        self.period = store.setting("period", "all")
        self.chat_open = store.setting("chat_open", False)
        self.chat_entry = None
        self.thread = None
        self.point = None
        self.image_origin = (0, 0)
        self.runs = {}
        self.language = store.setting("language", "en")
        root.title(self.t("window_title"))
        root.geometry(store.setting("geometry", "1120x760"))
        root.minsize(820, 540)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._icon()
        self.apply_chat_layout()
        root.bind("<Configure>", self.resize_chat, add="+")
        self.refresh_sessions()
        saved_session = store.setting("session")
        if saved_session in [entry['session'] for entry in self.sessions]:
            self.select_session(saved_session)
        elif self.sessions:
            self.select_session(self.sessions[0]["session"])
        else:
            self.draw()
        # Inside a presentation the side arrows turn slides; up/down always move through history.
        root.bind("<Left>", self.key(lambda: self.turn(-1) or self.step(1)))
        root.bind("<Right>", self.key(lambda: self.turn(1) or self.step(-1)))
        root.bind("<Up>", self.key(lambda: self.step(-1)))
        root.bind("<Down>", self.key(lambda: self.step(1)))
        root.bind("<Prior>", self.key(lambda: self.turn(-1)))
        root.bind("<Next>", self.key(lambda: self.turn(1)))
        root.bind("<Key-f>", self.key(self.set_fit))
        root.bind("<Key-1>", self.key(self.actual_size))
        root.bind("<Control-o>", lambda event: self.open_files())
        root.bind("<Control-plus>", lambda event: self.zoom(1.2))
        root.bind("<Control-equal>", lambda event: self.zoom(1.2))
        root.bind("<Control-minus>", lambda event: self.zoom(1 / 1.2))
        root.after(50, self.poll_events)
        root.after(900, self.watch)

    def t(self, key, **values):
        return i18n.text(self.language, key, **values)

    def set_language(self, language):
        """Rebuild the window in the other language, keeping project, selection, filter and chat."""
        if language == self.language:
            return
        self.language = language
        self.store.set_setting("language", language)
        for child in self.root.winfo_children():
            child.destroy()
        self.root.title(self.t("window_title"))
        self._build()
        self.apply_chat_layout()
        self.refresh_sessions()
        self.refresh_history()
        self.chat_entry = None
        if self.current:
            self.select(self.current, rebuild=False)
        elif self.session:
            self.clear_view()
        else:
            self.load_chat()
            self.draw()

    def key(self, action):
        """Window shortcuts, except while the user is typing a question."""
        return lambda event: None if self.root.focus_get() is self.chat_input else action()

    def _label(self, parent, text, fg=INK, bg=None, size=10, weight="normal", **kwargs):
        return tk.Label(parent, text=text, fg=fg, bg=bg or parent.cget("bg"),
                        font=("DejaVu Sans", size, weight), **kwargs)

    def _button(self, parent, text, command, primary=False, light=False):
        bg = ACCENT if primary else ("#e1e7df" if light else CARD)
        fg = DARK if primary or light else INK
        # A 2 px ring in the parent's colour, which turns visible when the button has keyboard focus.
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground="#a6e6d0" if primary else ("#d4ded3" if light else SELECTED),
                         activeforeground=fg, relief="flat", bd=0, highlightthickness=2,
                         highlightbackground=parent.cget("bg"), highlightcolor=DARK if light else (INK if primary else ACCENT),
                         cursor="hand2", font=("DejaVu Sans", 10), padx=13, pady=7)

    def _chip(self, parent, text, command, chosen=False, bold=False):
        """Small toggle-like button: filters, languages, history arrows."""
        return tk.Button(parent, text=text, command=command, relief="flat", bd=0, cursor="hand2",
                         font=("DejaVu Sans", 8, "bold" if bold else "normal"), padx=8, pady=3,
                         bg=ACCENT if chosen else CARD, fg=DARK if chosen else INK,
                         activebackground="#a6e6d0" if chosen else SELECTED, activeforeground=DARK if chosen else INK,
                         highlightthickness=2, highlightbackground=parent.cget("bg"), highlightcolor=INK if chosen else ACCENT)

    def _build(self):
        header = tk.Frame(self.root, bg=BG, height=78)
        header.pack(fill="x")
        header.pack_propagate(False)
        mark = tk.Canvas(header, width=40, height=40, bg=BG, highlightthickness=0)
        mark.pack(side="left", padx=(24, 13))
        mark.create_oval(3, 3, 37, 37, fill=ACCENT, outline="")
        mark.create_line(11, 14, 20, 28, 29, 14, fill=BG, width=3, capstyle="round", joinstyle="round")
        brand = tk.Frame(header, bg=BG)
        brand.pack(side="left")
        self._label(brand, "Visor", size=19, weight="bold", anchor="w").pack(anchor="w")
        self._label(brand, self.t("tagline"), fg=MUTED, size=9).pack(anchor="w")
        self._button(header, self.t("open_image"), self.open_files).pack(side="right", padx=22)
        self.ask_button = self._button(header, self.t("ask"), self.toggle_chat)
        self.ask_button.pack(side="right")
        languages = tk.Frame(header, bg=BG)
        languages.pack(side="right", padx=(0, 16))
        for code in i18n.LANGUAGES:
            chosen = code == self.language
            self._chip(languages, code.upper(), lambda value=code: self.set_language(value), chosen, bold=chosen).pack(side="left")
        tk.Frame(self.root, height=1, bg="#2a3b40").pack(fill="x")
        body = tk.Frame(self.root, bg=PANEL)
        body.pack(fill="both", expand=True)
        sidebar = tk.Frame(body, bg=PANEL, width=264)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._label(sidebar, self.t("project"), fg=MUTED, size=8, weight="bold").pack(anchor="w", padx=20, pady=(22, 10))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Visor.TCombobox", fieldbackground=CARD, background=CARD,
                        foreground=INK, arrowcolor=ACCENT, borderwidth=0, padding=7)
        style.map("Visor.TCombobox", fieldbackground=[("readonly", CARD)],
                  selectbackground=[("readonly", CARD)], selectforeground=[("readonly", INK)])
        style.configure("Visor.Vertical.TScrollbar", background=CARD, troughcolor=PANEL, bordercolor=PANEL,
                        lightcolor=CARD, darkcolor=CARD, arrowcolor=MUTED, gripcount=0)
        style.map("Visor.Vertical.TScrollbar", background=[("active", SELECTED)])
        self.session_var = tk.StringVar()
        self.session_box = ttk.Combobox(sidebar, textvariable=self.session_var, state="readonly",
                                       style="Visor.TCombobox", font=("DejaVu Sans", 10))
        self.session_box.pack(fill="x", padx=17)
        self.session_box.bind("<<ComboboxSelected>>", self.on_session_changed)
        tk.Checkbutton(sidebar, text=self.t("follow"), variable=self.follow, command=self.follow_changed,
            bg=PANEL, fg=INK, selectcolor=CARD, activebackground=PANEL, activeforeground=INK,
            font=("DejaVu Sans", 9), highlightthickness=2, highlightbackground=PANEL, highlightcolor=ACCENT, cursor="hand2").pack(anchor="w", padx=17, pady=15)
        history_title = tk.Frame(sidebar, bg=PANEL)
        history_title.pack(fill="x", padx=20, pady=(5, 10))
        self._label(history_title, self.t("history"), fg=MUTED, size=8, weight="bold").pack(side="left")
        # History moves with ↑ ↓ here; the arrows at the bottom of the figure are for slides only.
        self._chip(history_title, "↓", lambda: self.step(1)).pack(side="right")
        self._chip(history_title, "↑", lambda: self.step(-1)).pack(side="right", padx=(0, 3))
        self.count_label = self._label(history_title, "0", fg=MUTED, size=8)
        self.count_label.pack(side="right", padx=(0, 8))
        periods = tk.Frame(sidebar, bg=PANEL)
        periods.pack(fill="x", padx=17, pady=(0, 10))
        self.period_buttons = {}
        for key in PERIODS:
            button = self._chip(periods, self.t(f"period_{key}"), lambda value=key: self.set_period(value))
            button.pack(side="left", padx=(0, 3))
            self.period_buttons[key] = button
        self.paint_periods()
        hist_area = tk.Frame(sidebar, bg=PANEL)
        hist_area.pack(fill="both", expand=True, padx=(10, 0))
        self.history_canvas = tk.Canvas(hist_area, bg=PANEL, highlightthickness=0, bd=0, width=244)
        self.history_canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(hist_area, orient="vertical", command=self.history_canvas.yview, style="Visor.Vertical.TScrollbar")
        scroll.pack(side="right", fill="y")
        self.history_canvas.configure(yscrollcommand=scroll.set)
        self.history_frame = tk.Frame(self.history_canvas, bg=PANEL)
        self.history_window = self.history_canvas.create_window(0, 0, window=self.history_frame, anchor="nw")
        self.history_frame.bind("<Configure>", lambda event: self.history_canvas.configure(scrollregion=self.history_canvas.bbox("all")))
        self.history_canvas.bind("<Configure>", lambda event: self.history_canvas.itemconfigure(self.history_window, width=event.width))
        self._label(sidebar, self.t("local"), fg=MUTED, size=8).pack(anchor="w", padx=20, pady=18)
        main = self.main = tk.Frame(body, bg=PAPER)
        main.pack(side="left", fill="both", expand=True)
        self._build_chat(body)
        heading = tk.Frame(main, bg=PAPER)
        heading.pack(fill="x", padx=28, pady=(25, 15))
        self.title_label = self._label(heading, self.t("welcome_title"), fg=DARK, size=19, weight="bold", anchor="w",
                                       justify="left", wraplength=650)
        self.title_label.pack(fill="x")
        self.caption_label = self._label(heading, self.t("welcome_caption"), fg=SOFT, size=10, anchor="w", justify="left", wraplength=650)
        self.caption_label.pack(fill="x", pady=(7, 0))
        self.new_button = self._button(main, self.t("new_images"), self.show_latest, primary=True)
        self.canvas = tk.Canvas(main, bg="#e6ebe3", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True, padx=23)
        self.canvas.bind("<Configure>", self.schedule_draw)
        self.canvas.bind("<Button-4>", lambda event: self.zoom(1.15, event))
        self.canvas.bind("<Button-5>", lambda event: self.zoom(1 / 1.15, event))
        self.canvas.bind("<MouseWheel>", lambda event: self.zoom(1.15 if event.delta > 0 else 1 / 1.15, event))
        self.canvas.bind("<ButtonPress-1>", self.pan_start)
        self.canvas.bind("<B1-Motion>", self.pan_move)
        self.canvas.bind("<Double-Button-1>", lambda event: self.set_fit())
        self.canvas.bind("<Button-3>", self.mark)
        controls = tk.Frame(main, bg=PAPER)
        controls.pack(fill="x", padx=23, pady=(12, 7))
        self.slide_bar = tk.Frame(controls, bg=PAPER)
        self._button(self.slide_bar, "‹", lambda: self.turn(-1), light=True).pack(side="left")
        self.slide_label = self._label(self.slide_bar, "", fg=DARK, size=9, weight="bold", width=17)
        self.slide_label.pack(side="left")
        self._button(self.slide_bar, "›", lambda: self.turn(1), light=True).pack(side="left")
        self._button(controls, self.t("fit"), self.set_fit, light=True).pack(side="right")
        self._button(controls, "1:1", self.actual_size, light=True).pack(side="right", padx=5)
        self._button(controls, "+", lambda: self.zoom(1.2), light=True).pack(side="right")
        self.zoom_label = self._label(controls, "100 %", fg=DARK, size=9, width=7)
        self.zoom_label.pack(side="right")
        self._button(controls, "−", lambda: self.zoom(1 / 1.2), light=True).pack(side="right")
        self.detail_label = self._label(main, self.t("idle_detail"), fg=FAINT, size=8, anchor="w")
        self.detail_label.pack(fill="x", padx=25, pady=(2, 16))

    def _build_chat(self, body):
        self.chat = tk.Frame(body, bg=PANEL, width=380)
        self.chat.pack_propagate(False)
        self._label(self.chat, self.t("chat_title"), fg=MUTED, size=8, weight="bold").pack(anchor="w", padx=18, pady=(22, 6))
        self.chat_status = self._label(self.chat, "", fg=MUTED, size=8, anchor="w", justify="left", wraplength=340)
        self.chat_status.pack(fill="x", padx=18)
        actions = self.chat_actions = tk.Frame(self.chat, bg=PANEL)
        self._button(actions, self.t("new_thread"), self.new_thread).pack(side="left")
        self.copy_button = self._button(actions, self.t("copy_command"), self.copy_command)
        self.copy_button.pack(side="left", padx=6)
        bottom = self.chat_bottom = tk.Frame(self.chat, bg=PANEL)
        self.point_bar = tk.Frame(bottom, bg=PANEL)
        self.point_label = self._label(self.point_bar, "", fg=ACCENT, size=8, anchor="w")
        self.point_label.pack(side="left")
        tk.Button(self.point_bar, text=self.t("remove"), command=self.clear_point, bg=PANEL, fg=MUTED, activebackground=PANEL,
                  activeforeground=INK, relief="flat", bd=0, highlightthickness=2, highlightbackground=PANEL,
                  highlightcolor=ACCENT, cursor="hand2",
                  font=("DejaVu Sans", 8, "underline")).pack(side="right")
        self.chat_input = tk.Text(bottom, height=4, wrap="word", bg=CARD, fg=INK, insertbackground=INK, relief="flat",
                                  font=("DejaVu Sans", 10), padx=8, pady=6, highlightthickness=1,
                                  highlightbackground="#2a3b40", highlightcolor=ACCENT)
        self.chat_input.pack(fill="x")
        self.chat_input.bind("<Return>", self.chat_return)
        self.send_button = self._button(bottom, self.t("send"), self.send_or_stop, primary=True)
        self.send_button.pack(anchor="e", pady=(8, 0))
        self.chat_log = tk.Text(self.chat, wrap="word", bg=CARD, fg=INK, relief="flat", font=("DejaVu Sans", 10),
                                padx=10, pady=10, state="disabled", highlightthickness=0, cursor="arrow", takefocus=0)
        self.chat_log.tag_configure("user", foreground=ACCENT, font=("DejaVu Sans", 10, "bold"), spacing1=8)
        self.chat_log.tag_configure("assistant", spacing1=4)
        self.chat_log.tag_configure("tool", foreground=MUTED, font=("DejaVu Sans", 8))
        self.chat_log.tag_configure("error", foreground=ERROR)
        self.chat_log.tag_configure("note", foreground=MUTED, font=("DejaVu Sans", 9, "italic"))
        self.chat_log.tag_configure("bold", font=("DejaVu Sans", 10, "bold"))
        self.chat_log.tag_configure("code", font=("DejaVu Sans Mono", 9), foreground="#cfe6dd")

    def show_chat_controls(self, visible):
        """Without a session to ask, the panel shows only its explanation."""
        for widget in (self.chat_actions, self.chat_bottom, self.chat_log):
            widget.pack_forget()
        if visible:
            self.chat_actions.pack(fill="x", padx=16, pady=(10, 8))
            self.chat_bottom.pack(side="bottom", fill="x", padx=16, pady=(8, 16))
            self.chat_log.pack(fill="both", expand=True, padx=16)

    def apply_chat_layout(self):
        if self.chat_open:
            self.root.minsize(CHAT_MIN_WINDOW, 540)
            self.root.update_idletasks()
            width, height = self.root.winfo_width(), self.root.winfo_height()
            if width < CHAT_MIN_WINDOW:
                self.root.geometry(f"{CHAT_MIN_WINDOW}x{max(540, height)}")
            self.set_chat_width(self.chat_width(max(width, CHAT_MIN_WINDOW)))
            self.chat.pack(side="right", fill="y", before=self.main)
        else:
            self.root.minsize(820, 540)
            self.chat.pack_forget()
        chosen = self.chat_open
        self.ask_button.configure(bg=ACCENT if chosen else CARD, fg=DARK if chosen else INK,
                                  activebackground="#a6e6d0" if chosen else SELECTED, activeforeground=DARK if chosen else INK,
                                  highlightcolor=INK if chosen else ACCENT)

    @staticmethod
    def chat_width(window_width):
        return max(300, min(380, int(window_width * .3)))

    def resize_chat(self, event):
        if event.widget is self.root and self.chat_open:
            width = self.chat_width(event.width)
            if int(self.chat.cget("width")) != width:
                self.set_chat_width(width)

    def set_chat_width(self, width):
        # The status text wraps inside the panel, whatever width the panel has.
        self.chat.configure(width=width)
        self.chat_status.configure(wraplength=width - 40)

    def _icon(self):
        icon = Image.new("RGBA", (64, 64), BG)
        draw = ImageDraw.Draw(icon)
        draw.rounded_rectangle((5, 5, 59, 59), radius=17, fill=ACCENT)
        draw.line([(18, 23), (32, 44), (46, 23)], fill=BG, width=6, joint="curve")
        self.window_icon = ImageTk.PhotoImage(icon)
        self.root.iconphoto(True, self.window_icon)

    def refresh_sessions(self):
        self.sessions = self.store.sessions()
        # Keep full values separate so same-named directories cannot collide.
        self.session_labels = []
        for row in self.sessions:
            value = row["session"]
            if value.startswith("/"):
                path = Path(value)
                label = f"{path.name}  ·  {path.parent}"
            else:
                label = value
            self.session_labels.append(label)
        self.session_box["values"] = self.session_labels
        if not self.sessions:
            self.session_var.set(self.t("no_projects"))
        for i, row in enumerate(self.sessions):
            if row["session"] == self.session:
                self.session_box.current(i)
                break

    def on_session_changed(self, event=None):
        index = self.session_box.current()
        if index >= 0:
            self.select_session(self.sessions[index]["session"])

    def select_session(self, session):
        self.session = session
        self.store.set_setting("session", session)
        self.refresh_sessions()
        self.refresh_history()
        if self.items:
            self.select(self.items[0], rebuild=False)
        elif self.current:
            self.clear_view()  # nothing of this project in the chosen period
        self.new_button.pack_forget()

    def clear_view(self):
        """Nothing of this project falls in the chosen period: show an empty canvas, not another project's image."""
        if self.original:
            self.original.close()
        self.current, self.original, self.pages, self.point = None, None, [], None
        self.show_point()
        self.title_label.configure(text=self.t("nothing_title"))
        self.caption_label.configure(text=self.t("nothing_caption"))
        self.detail_label.configure(text="")
        self.count_label.configure(text=str(len(self.items)))
        self.slide_bar.pack_forget()
        self.load_chat()
        self.draw()

    def since(self):
        if self.period == "hour":
            return time.time() - 3600
        if self.period == "today":
            return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return None

    def set_period(self, period):
        self.period = period
        self.store.set_setting("period", period)
        self.paint_periods()
        self.refresh_history()
        # Keep the canvas consistent with the list: never show an image the filter hides.
        if self.current and not any(entry(item) == entry(self.current) for item in self.items):
            if self.items:
                self.select(self.items[0], rebuild=False)
            else:
                self.clear_view()

    def paint_periods(self):
        for key, button in self.period_buttons.items():
            chosen = key == self.period
            button.configure(bg=ACCENT if chosen else CARD, fg=DARK if chosen else INK,
                             activebackground="#a6e6d0" if chosen else SELECTED, activeforeground=DARK if chosen else INK)

    def refresh_history(self):
        self.items = self.store.history(self.session, 200, collapse=True, since=self.since()) if self.session else []
        self.count_label.configure(text=str(len(self.items)))
        for child in self.history_frame.winfo_children():
            child.destroy()
        self.thumbnails.clear()
        self.cards = {}
        versions = self.store.versions(self.session) if self.session else {}
        for index, item in enumerate(self.items):
            active = self.current and entry(item) == entry(self.current)
            bg = SELECTED if active else CARD
            card = tk.Frame(self.history_frame, bg=bg, padx=9, pady=9, cursor="hand2")
            card.pack(fill="x", padx=(4, 9), pady=(0, 7))
            self.cards[entry(item)] = card
            top = tk.Frame(card, bg=bg)
            top.pack(fill="x")
            thumbpath = Path(item["snapshot"]).with_suffix(".thumb.png")
            try:
                with Image.open(thumbpath if thumbpath.exists() else item["snapshot"]) as thumb:
                    thumb.thumbnail((62, 45))
                    photo = ImageTk.PhotoImage(thumb.copy())
                self.thumbnails.append(photo)
                label = tk.Label(top, image=photo, bg=bg, width=66, height=48)
                label.pack(side="left", padx=(0, 8))
            except (OSError, ValueError):
                self._label(top, "◇", fg=ACCENT, bg=bg, size=22).pack(side="left", padx=10)
            text = tk.Frame(top, bg=bg)
            text.pack(side="left", fill="x", expand=True)
            self._label(text, item["title"], bg=bg, size=9, weight="bold", anchor="w", wraplength=116, justify="left").pack(anchor="w")
            stamp = datetime.fromtimestamp(item["created"]).strftime("%d %b · %H:%M:%S")
            self._label(text, stamp, fg=MUTED, bg=bg, size=8).pack(anchor="w", pady=(4, 0))
            parts = [self.t("latest")] if index == 0 else []
            number, total = versions.get(entry(item), (1, 1))
            if total > 1:  # a version only when the same file was shown more than once
                parts.append(self.t("version", n=number))
            if item.get("deck"):
                parts.append(self.t("slides_one") if item["pages"] == 1 else self.t("slides_many", n=item["pages"]))
            if parts:
                self._label(card, "  ·  ".join(parts), fg=ACCENT if index == 0 else MUTED,
                            bg=bg, size=7, anchor="w").pack(anchor="w", pady=(6, 0))
            def bind_tree(widget, chosen=item):
                widget.bind("<Button-1>", lambda event, value=chosen: self.select(value))
                widget.bind("<Button-4>", lambda event: self.history_canvas.yview_scroll(-3, "units"))
                widget.bind("<Button-5>", lambda event: self.history_canvas.yview_scroll(3, "units"))
                for child in widget.winfo_children():
                    bind_tree(child, chosen)
            bind_tree(card)
        if self.session and not self.items:
            self._label(self.history_frame, self.t("nothing_history"), fg=MUTED, size=9).pack(anchor="w", padx=10, pady=6)

    def select(self, item, rebuild=True):
        if self.original:
            self.original.close()
        self.current = item
        self.point = None
        self.show_point()
        if not item.get("deck"):
            self.pages = []
        elif not self.pages or self.pages[0]["deck"] != item["deck"]:
            self.pages = self.store.deck_pages(item["deck"])
        try:
            with Image.open(item["snapshot"]) as image:
                self.original = image.convert("RGBA")
        except (OSError, ValueError):
            self.original = None
        self.fit = True
        self.pan = [0, 0]
        self.watch_candidate = None
        self.title_label.configure(text=item["title"])
        self.caption_label.configure(text=item["caption"] or Path(item["source"]).name)
        self.root.title(f"{item['title']} · Visor")
        source = Path(item["source"])
        # File name before folder, so a long path truncates the folder and not the name.
        self.detail_label.configure(text=f"{item['width']} × {item['height']} px   ·   {source.name}   ·   {source.parent}")
        if self.pages:
            self.slide_label.configure(text=self.t("slide", page=item["page"], pages=len(self.pages)))
            self.slide_bar.pack(side="left", padx=(24, 0))
        else:
            self.slide_bar.pack_forget()
        if rebuild:
            self.refresh_history()
        else:
            for key, card in self.cards.items():
                color = SELECTED if key == entry(item) else CARD
                self._recolor(card, color)
        index = next((i for i, value in enumerate(self.items) if entry(value) == entry(item)), 0)
        shown = any(entry(value) == entry(item) for value in self.items)
        self.count_label.configure(text=f"{len(self.items) - index} / {len(self.items)}" if shown else str(len(self.items)))
        if self.chat_entry != entry(item):
            self.load_chat()
        self.draw()

    def _recolor(self, widget, color):
        widget.configure(bg=color)
        for child in widget.winfo_children():
            self._recolor(child, color)

    def schedule_draw(self, event=None):
        if self.pending_draw:
            self.root.after_cancel(self.pending_draw)
        self.pending_draw = self.root.after(45, self.draw)
        self.caption_label.configure(wraplength=max(300, self.canvas.winfo_width() - 20))
        self.title_label.configure(wraplength=max(240, self.canvas.winfo_width() - 20))

    def draw(self):
        self.pending_draw = None
        canvas = self.canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if not self.original:
            canvas.create_oval(width / 2 - 28, height / 2 - 70, width / 2 + 28, height / 2 - 14, fill="#d3e3d6", outline="")
            canvas.create_text(width / 2, height / 2 - 42, text="V", fill="#507d65", font=("DejaVu Sans", 24, "bold"))
            canvas.create_text(width / 2, height / 2 + 10, text=self.t("empty_canvas"), fill=DARK, font=("DejaVu Sans", 15, "bold"))
            canvas.create_text(width / 2, height / 2 + 46, text=self.t("empty_canvas_hint"), fill="#596c5d", font=("DejaVu Sans", 10))
            return
        if self.fit:
            self.scale = min((max(width, 80) - 36) / self.original.width,
                             (max(height, 80) - 36) / self.original.height, 1.0)
            self.pan = [0, 0]
        # Render only the visible crop: zooming a large image never allocates a giant bitmap.
        scaled_w, scaled_h = self.original.width * self.scale, self.original.height * self.scale
        left = (width - scaled_w) / 2 + self.pan[0]
        top = (height - scaled_h) / 2 + self.pan[1]
        self.image_origin = (left, top)
        canvas.create_rectangle(left + 3, top + 4, left + scaled_w + 3, top + scaled_h + 4, fill="#cdd5ca", outline="")
        canvas.create_rectangle(left, top, left + scaled_w, top + scaled_h, fill="white", outline="")
        x0 = max(0, -left / self.scale)
        y0 = max(0, -top / self.scale)
        x1 = min(self.original.width, (width - left) / self.scale)
        y1 = min(self.original.height, (height - top) / self.scale)
        if x1 > x0 and y1 > y0:
            box = (int(x0), int(y0), min(self.original.width, int(x1) + 1), min(self.original.height, int(y1) + 1))
            crop = self.original.crop(box)
            size = (max(1, round(crop.width * self.scale)), max(1, round(crop.height * self.scale)))
            rendered = crop.resize(size, Image.Resampling.LANCZOS)
            self.photo = ImageTk.PhotoImage(rendered)
            canvas.create_image(left + box[0] * self.scale, top + box[1] * self.scale, image=self.photo, anchor="nw")
        if self.point:
            x, y = left + self.point[0] * self.scale, top + self.point[1] * self.scale
            canvas.create_oval(x - 11, y - 11, x + 11, y + 11, outline="white", width=6, tags="marker")
            canvas.create_oval(x - 11, y - 11, x + 11, y + 11, outline="#e5484d", width=3, tags="marker")
        self.zoom_label.configure(text=f"{round(self.scale * 100)} %")

    def set_fit(self):
        self.fit = True
        self.draw()

    def actual_size(self):
        self.fit = False
        self.scale = 1
        self.pan = [0, 0]
        self.draw()

    def zoom(self, factor, event=None):
        if not self.original:
            return
        old = self.scale
        self.scale = max(0.02, min(8.0, old * factor))
        self.fit = False
        ratio = self.scale / old
        dx = event.x - self.canvas.winfo_width() / 2 if event else 0
        dy = event.y - self.canvas.winfo_height() / 2 if event else 0
        self.pan = [dx - (dx - self.pan[0]) * ratio, dy - (dy - self.pan[1]) * ratio]
        self.draw()

    def pan_start(self, event):
        self.canvas.focus_set()
        self.drag_origin = (event.x, event.y, self.pan.copy())

    def pan_move(self, event):
        if not hasattr(self, "drag_origin"):
            return
        x, y, pan = self.drag_origin
        self.fit = False
        self.pan = [pan[0] + event.x - x, pan[1] + event.y - y]
        self.draw()

    def turn(self, direction):
        """Move within the current presentation. Returns False when no presentation is shown."""
        if not self.pages or not self.current:
            return False
        target = self.current["page"] - 1 + direction
        if 0 <= target < len(self.pages):
            self.select(self.pages[target], rebuild=False)
        return True

    def step(self, direction):
        if not self.items or not self.current:
            return
        index = next((i for i, item in enumerate(self.items) if entry(item) == entry(self.current)), 0)
        target = max(0, min(len(self.items) - 1, index + direction))
        self.select(self.items[target])
        self.history_canvas.yview_moveto(target / max(1, len(self.items)))

    def follow_changed(self):
        self.store.set_setting("follow", self.follow.get())
        if self.follow.get():
            self.show_latest()

    def show_latest(self):
        sessions = self.store.sessions()
        if sessions:
            self.select_session(sessions[0]["session"])
        self.new_button.pack_forget()

    def open_files(self):
        paths = filedialog.askopenfilenames(title=self.t("dialog_title"), filetypes=[
            (self.t("dialog_images"), "*.pdf *.png *.jpg *.jpeg *.webp *.gif *.tif *.tiff *.bmp *.ico"), (self.t("dialog_all"), "*")])
        if paths:
            self.ingest_in_background({"paths": list(paths), "session": self.session or "Images"})

    def ingest_in_background(self, payload, watched_id=None):
        def worker():
            try:
                items = self.store.ingest(payload["paths"], payload["session"], payload.get("title", ""), payload.get("caption", ""),
                                          payload.get("deck", False), payload.get("agent_session"), payload.get("agent_cwd"))
                self.events.put(({"action": "received", "items": items, "focus": payload.get("focus", False), "watched_id": watched_id}, None, None))
            except Exception as exc:
                self.events.put(({"action": "notice", "message": str(exc)}, None, None))
            finally:
                self.events.put(({"action": "watch_done"}, None, None))
        threading.Thread(target=worker, daemon=True).start()

    def watch(self):
        try:
            if self.follow.get() and self.current and not self.watch_busy:
                item = self.current
                # A presentation is watched as a whole: every source file it was made from.
                sources = []
                for row in self.pages or [item]:
                    if not sources or sources[-1][0] != row["source"]:
                        sources.append((row["source"], row["signature"]))
                if all(self.store.latest_for_source(item["session"], source) == entry(item) for source, _ in sources):
                    values = []
                    for source, _ in sources:
                        try:
                            values.append(signature(Path(source)))
                        except OSError:
                            values.append(None)
                    if all(values) and values != [stored for _, stored in sources]:
                        candidate = (entry(item), tuple(values))
                        if candidate == self.watch_candidate:
                            self.watch_busy = True
                            self.ingest_in_background({"paths": [source for source, _ in sources], "session": item["session"],
                                "title": item["title"], "caption": item["caption"], "deck": bool(item.get("deck")),
                                "agent_session": item.get("agent_session"), "agent_cwd": item.get("agent_cwd")}, entry(item))
                            self.watch_candidate = None
                        else:
                            self.watch_candidate = candidate
                    else:
                        self.watch_candidate = None
        finally:
            self.root.after(900, self.watch)

    def mark(self, event):
        """Right click: point at an element of the figure to ask about it."""
        if not self.original:
            return
        x = (event.x - self.image_origin[0]) / self.scale
        y = (event.y - self.image_origin[1]) / self.scale
        if not (0 <= x < self.original.width and 0 <= y < self.original.height):
            return
        if not self.chat_open:
            self.toggle_chat()
        if not self.current.get("agent_session"):
            return
        self.point = (int(x), int(y))
        self.show_point()
        self.draw()
        self.chat_input.focus_set()

    def show_point(self):
        if self.point:
            self.point_label.configure(text=self.t("point", x=self.point[0], y=self.point[1]))
            self.point_bar.pack(fill="x", pady=(0, 6), before=self.chat_input)
        else:
            self.point_bar.pack_forget()

    def clear_point(self):
        self.point = None
        self.show_point()
        self.draw()

    def toggle_chat(self):
        self.chat_open = not self.chat_open
        self.store.set_setting("chat_open", self.chat_open)
        self.apply_chat_layout()
        if self.chat_open and self.current and self.current.get("agent_session"):
            self.chat_input.focus_set()
        self.schedule_draw()

    def load_chat(self):
        """Show the newest thread of the selected entry."""
        item = self.current
        self.chat_entry = entry(item) if item else None
        self.thread = self.store.thread(self.chat_entry) if item else None
        self.chat_log.configure(state="normal")
        self.chat_log.delete("1.0", "end")
        self.chat_log.configure(state="disabled")
        self.show_chat_controls(bool(item and item.get("agent_session")))
        if not item:
            self.chat_status.configure(text=self.t("chat_pick"))
        elif not item.get("agent_session"):
            self.chat_status.configure(text=self.t("chat_no_session"))
        else:
            self.chat_status.configure(text=self.t("chat_fork", id=item["agent_session"][:8]))
            messages = self.store.messages(self.thread["id"]) if self.thread else []
            for message in messages:
                self.append_chat(message["role"], message["text"])
            if not messages:
                self.append_chat("note", self.t("chat_hint"))
        self.update_chat_buttons()

    def append_chat(self, role, text):
        prefix = {"user": self.t("you"), "tool": "·  ", "error": "⚠  "}.get(role, "")
        self.chat_log.configure(state="normal")
        self.chat_log.insert("end", prefix, role)
        # Answers come as light Markdown: render **bold** and `code`, leave the rest as written.
        for part in INLINE.split(text) if role == "assistant" else [text]:
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                self.chat_log.insert("end", part[2:-2], (role, "bold"))
            elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                self.chat_log.insert("end", part[1:-1], (role, "code"))
            else:
                self.chat_log.insert("end", part, role)
        self.chat_log.insert("end", "\n\n", role)
        self.chat_log.configure(state="disabled")
        self.chat_log.see("end")

    def update_chat_buttons(self):
        usable = bool(self.current and self.current.get("agent_session"))
        running = bool(self.thread and self.thread["id"] in self.runs)
        self.send_button.configure(text=self.t("stop") if running else self.t("send"), state="normal" if usable else "disabled")
        self.chat_input.configure(state="normal" if usable else "disabled")
        self.copy_button.configure(state="normal" if self.thread and self.thread["fork"] else "disabled")

    def chat_return(self, event):
        if event.state & 0x1:  # Shift+Enter keeps a new line
            return None
        self.send_or_stop()
        return "break"

    def new_thread(self):
        if self.current and self.current.get("agent_session"):
            self.store.new_thread(entry(self.current))
            self.load_chat()

    def copy_command(self):
        if not (self.thread and self.thread["fork"] and self.current):
            return
        cwd = chat.session_cwd(self.current["agent_session"], self.current.get("agent_cwd"))
        line = f"cd {shlex.quote(cwd)} && claude --resume {self.thread['fork']}"
        self.root.clipboard_clear()
        self.root.clipboard_append(line)
        self.chat_status.configure(text=self.t("copied", line=line))

    def send_or_stop(self):
        item = self.current
        if not (item and item.get("agent_session")):
            return
        if self.thread and self.thread["id"] in self.runs:
            self.runs[self.thread["id"]].terminate()
            return
        question = self.chat_input.get("1.0", "end").strip()
        if not question:
            return
        thread = self.thread or self.store.new_thread(entry(item))
        self.thread = thread
        if not self.store.messages(thread["id"]):
            self.chat_log.configure(state="normal")
            self.chat_log.delete("1.0", "end")
            self.chat_log.configure(state="disabled")
        self.chat_input.delete("1.0", "end")
        point, self.point = self.point, None
        shown = f"◉ ({point[0]}, {point[1]})  {question}" if point else question
        self.store.add_message(thread["id"], "user", shown)
        self.append_chat("user", shown)
        if point:
            self.show_point()
            self.draw()
        try:
            if point:
                marked, zoom = chat.mark_point(item["snapshot"], *point, self.store.directory / "points")
                point = dict(x=point[0], y=point[1], marked=marked, zoom=zoom)
            readable = [self.store.assets, self.store.directory / "points"]
            args = chat.command(item["agent_session"], thread["fork"], chat.prompt(question, item, not thread["fork"], point), readable)
            errors = tempfile.TemporaryFile()
            process = subprocess.Popen(args, cwd=chat.session_cwd(item["agent_session"], item.get("agent_cwd")),
                                       env=chat.clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=errors, text=True, bufsize=1)
        except (OSError, RuntimeError) as exc:
            self.store.add_message(thread["id"], "error", str(exc))
            self.append_chat("error", str(exc))
            return
        self.runs[thread["id"]] = process
        self.update_chat_buttons()
        threading.Thread(target=self.chat_worker, args=(thread["id"], process, errors), daemon=True).start()

    def chat_worker(self, thread, process, errors):
        """Reads the fork's stream off the Tk thread; the window only receives finished events."""
        finished = False
        try:
            for line in process.stdout:
                for kind, text in chat.events(line):
                    if kind == "session":
                        self.store.set_fork(thread, text)
                    elif kind in ("text", "tool", "error"):
                        self.store.add_message(thread, "assistant" if kind == "text" else kind, text)
                    finished = finished or kind == "done"
                    self.events.put(({"action": "chat", "thread": thread, "kind": kind, "text": text}, None, None))
            process.wait()
            if not finished:
                errors.seek(0)
                detail = errors.read().decode("utf-8", "replace").strip()[-600:]
                message = self.t("stopped") if process.returncode and process.returncode < 0 else (detail or self.t("exit_code", code=process.returncode))
                self.store.add_message(thread, "error", message)
                self.events.put(({"action": "chat", "thread": thread, "kind": "error", "text": message}, None, None))
        finally:
            process.stdout.close()
            errors.close()
            self.events.put(({"action": "chat", "thread": thread, "kind": "end", "text": ""}, None, None))

    def dispatch(self, payload):
        action = payload["action"]
        if action == "status":
            return {"ok": True, "running": True, "pid": os.getpid(), "session": self.session,
                    "current_id": self.current["id"] if self.current else None, "following": self.follow.get()}
        if action == "stop":
            self.root.after(120, self.close)
            return {"ok": True, "running": False}
        if action in ("show", "received"):
            items = payload["items"]
            watched_id = payload.get("watched_id")
            # Show the last entry received: a single image, or the first slide of a presentation.
            shown = [item for item in items if entry(item) == entry(items[-1])]
            target = shown[0]
            self.refresh_sessions()
            if not self.current or (self.follow.get() and (not watched_id or entry(self.current) == watched_id)):
                if watched_id and self.current and self.current.get("deck") and target.get("deck"):
                    target = shown[min(self.current["page"], len(shown)) - 1]
                self.session = target["session"]
                self.store.set_setting("session", self.session)
                self.current = target
                self.refresh_sessions()
                self.refresh_history()
                self.select(target, rebuild=False)
                self.history_canvas.yview_moveto(0)
                self.new_button.pack_forget()
            else:
                self.refresh_history()
                self.new_button.pack(before=self.canvas, padx=23, pady=(0, 12), anchor="w")
            if payload.get("focus"):
                self.root.deiconify()
                self.root.lift()
            return {"ok": True, "running": True, "pid": os.getpid(), "session": items[-1]["session"],
                    "shown": len(items), "item_ids": [item["id"] for item in items], "current_id": self.current["id"],
                    "queued": self.current["id"] != target["id"]}
        if action == "open":
            if payload.get("session"):
                if payload["session"] not in [row["session"] for row in self.sessions]:
                    raise ValueError("That project has no images yet.")
                self.select_session(payload["session"])
            self.root.deiconify()
            self.root.lift()
            return self.dispatch({"action": "status"})
        if action == "chat":
            if payload["kind"] == "end":
                self.runs.pop(payload["thread"], None)
            if self.thread and self.thread["id"] == payload["thread"]:
                if payload["kind"] == "session":
                    self.thread["fork"] = payload["text"]
                elif payload["kind"] in ("text", "tool", "error"):
                    self.append_chat("assistant" if payload["kind"] == "text" else payload["kind"], payload["text"])
                self.update_chat_buttons()
        if action == "notice":
            self.detail_label.configure(text=payload["message"])
        if action == "watch_done":
            self.watch_busy = False
        return {"ok": True}

    def poll_events(self):
        for _ in range(30):
            try:
                payload, done, result = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                response = self.dispatch(payload)
            except Exception as exc:
                traceback.print_exc()
                response = {"ok": False, "error": str(exc)}
            if done is not None:
                result.update(response)
                done.set()
        self.root.after(50, self.poll_events)

    def close(self):
        for process in list(self.runs.values()):
            process.terminate()
        self.store.set_setting("geometry", self.root.geometry())
        self.store.set_setting("follow", self.follow.get())
        self.root.destroy()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(20)
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                raise ValueError("Incomplete or oversized request.")
            payload = json.loads(line)
            if not isinstance(payload, dict) or payload.get("action") not in {"status", "show", "open", "stop"}:
                raise ValueError("Unknown action.")
            if payload["action"] == "show":
                paths = payload.get("paths")
                if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                    raise ValueError("paths must be a list of paths.")
                for key in ("session", "title", "caption"):
                    if key in payload and not isinstance(payload[key], str):
                        raise ValueError(f"{key} must be a string.")
                if not isinstance(payload.get("deck", False), bool):
                    raise ValueError("deck must be true or false.")
                for key in ("agent_session", "agent_cwd"):
                    if payload.get(key) is not None and not isinstance(payload[key], str):
                        raise ValueError(f"{key} must be a string.")
                payload["items"] = self.server.app.store.ingest(paths, payload.get("session", "Images"), payload.get("title", ""),
                    payload.get("caption", ""), payload.get("deck", False), payload.get("agent_session"), payload.get("agent_cwd"))
            done, result = threading.Event(), {}
            self.server.app.events.put((payload, done, result))
            if not done.wait(16):
                raise RuntimeError("The window is busy. Check visor status.")
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        try:
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
        except OSError:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def run():
    root = tk.Tk(className="AgentVisor")
    app = Visor(root, Store())
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    server = Server(socket_path(), Handler)
    os.chmod(socket_path(), 0o600)
    server.app = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        root.mainloop()
    finally:
        server.shutdown()
        server.server_close()
        Path(socket_path()).unlink(missing_ok=True)
