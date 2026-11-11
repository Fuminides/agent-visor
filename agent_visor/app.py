"""Native Tk window. All widget operations stay on the Tk event thread."""
from __future__ import annotations

from datetime import datetime
import json
import math
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
from tkinter import filedialog, font as tkfont, ttk
import traceback

from PIL import Image, ImageDraw, ImageOps, ImageTk

from . import chat, codex, i18n, mirror, runs
from .ipc import MAX_REQUEST, runtime_dir, socket_path
from .storage import Store, conversation, signature

# The window's colours. Each theme gives the same names a value, and use_theme() makes one current by
# setting them as module globals, which the window reads whenever it is built:
#   BG header and the sheet around the figure · PANEL sidebars · CARD filled controls and the hovered row ·
#   PRESSED a control under the pointer · SELECTED the chosen row · LINE dividers · FIELD text boxes ·
#   EDGE thumbnail outlines and scrollbars · STAGE behind the figure · SHADOWS under the figure ·
#   INK text · MUTED secondary text · ACCENT primary buttons and focus · ACCENT_HOT under the pointer ·
#   ON_ACCENT text on the accent · ACCENT_TEXT accent-coloured text · CHOSEN_HOT a chosen pill under the pointer.
# Text colours keep at least 4.5:1 contrast on every surface they sit on (checked in the tests).
THEMES = {
    "sand": dict(  # warm, slightly orange paper
        BG="#f0eee6", PANEL="#e8e5da", CARD="#dedacd", PRESSED="#d1ccbd", SELECTED="#d6d1c2", LINE="#d9d5c8",
        FIELD="#faf9f5", EDGE="#c9c4b5", STAGE="#e4e1d5", SHADOWS=("#dfdcd0", "#d8d4c7", "#cdc9ba"),
        INK="#1f1e1b", MUTED="#55534d", ACCENT="#b85636", ACCENT_HOT="#a64a2c", ON_ACCENT="#ffffff",
        ACCENT_TEXT="#8f3f22", CHOSEN_HOT="#3b3a36", ERROR="#a3231c"),
    "stone": dict(  # neutral grey and white
        BG="#f1f1f0", PANEL="#e7e7e5", CARD="#dcdcd9", PRESSED="#cfcfcb", SELECTED="#d3d3cf", LINE="#d8d8d4",
        FIELD="#ffffff", EDGE="#c6c6c1", STAGE="#e3e3e0", SHADOWS=("#dededb", "#d7d7d3", "#ccccc7"),
        INK="#1d1d1f", MUTED="#545458", ACCENT="#0a66d8", ACCENT_HOT="#0957ba", ON_ACCENT="#ffffff",
        ACCENT_TEXT="#0a55b5", CHOSEN_HOT="#3a3a3d", ERROR="#a3231c"),
    "sky": dict(  # cool, slightly blue
        BG="#edf1f6", PANEL="#e2e8f0", CARD="#d5dde8", PRESSED="#c6d0de", SELECTED="#cbd6e4", LINE="#d3dbe6",
        FIELD="#f8fafc", EDGE="#bcc7d6", STAGE="#dfe6ee", SHADOWS=("#d9e0e9", "#d1d9e4", "#c5cfdc"),
        INK="#18212c", MUTED="#4b5665", ACCENT="#2a62b8", ACCENT_HOT="#22539e", ON_ACCENT="#ffffff",
        ACCENT_TEXT="#1f4f99", CHOSEN_HOT="#33404f", ERROR="#a3231c"),
    "dark": dict(
        BG="#262624", PANEL="#1f1e1d", CARD="#30302e", PRESSED="#3d3d3a", SELECTED="#383835", LINE="#34332f",
        FIELD="#30302e", EDGE="#4a4945", STAGE="#191817", SHADOWS=("#161514", "#131211", "#0f0e0e"),
        INK="#f3f1ea", MUTED="#b3b0a6", ACCENT="#d97757", ACCENT_HOT="#e38b6e", ON_ACCENT="#1f1e1b",
        ACCENT_TEXT="#e8997a", CHOSEN_HOT="#d9d6cc", ERROR="#ff9b8a"),
}
SWATCHES = {"sand": "#d97757", "stone": "#c4c4c0", "sky": "#6f9bd8", "dark": "#2b2a28"}  # the dots that choose a theme
ICON = ("#b85636", "#ffffff")  # the app icon keeps its colours in every theme
PERIODS = ("all", "today", "hour")


def use_theme(name):
    theme = THEMES.get(name, THEMES["sand"])
    globals().update(theme)
    # Button looks: fill, fill under the pointer, text.
    globals()["KINDS"] = {"plain": (theme["CARD"], theme["PRESSED"], theme["INK"]),
                          "primary": (theme["ACCENT"], theme["ACCENT_HOT"], theme["ON_ACCENT"]),
                          "chosen": (theme["INK"], theme["CHOSEN_HOT"], theme["BG"]),
                          "quiet": (None, theme["CARD"], theme["MUTED"])}


use_theme("sand")
SERIFS = ("Source Serif 4", "Source Serif Pro", "Noto Serif", "Bitstream Charter", "DejaVu Serif")
# With the question panel open the window needs room for sidebar, panel and a usable figure.
CHAT_MIN_WINDOW = 1080
SIDEBAR = 272
INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)")


def visor_mark(size, fill, stroke, background=None):
    """The Visor mark (a V in a circle, the same proportions as the app icon in assets/visor.svg), drawn at 4x
    and reduced so its curves stay smooth; Tk's own canvas shapes are not antialiased."""
    scale = 4
    side = size * scale
    image = Image.new("RGBA", (side, side), background or (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((side * .075, side * .075, side * .925, side * .925), fill=fill)
    points, width = [(side * .275, side * .35), (side * .5, side * .7), (side * .725, side * .35)], side * .075
    draw.line(points, fill=stroke, width=round(width), joint="curve")
    for x, y in (points[0], points[-1]):  # round caps
        draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=stroke)
    return image.resize((size, size), Image.Resampling.BOX)  # area average: smooth edges without a halo


def tile(path, width, height, radius, edge):
    """A thumbnail that fills width x height, with rounded corners that let the row's colour through
    and a hairline that keeps a white figure visible on a light row."""
    with Image.open(path) as source:
        image = source.convert("RGBA")
    image = Image.alpha_composite(Image.new("RGBA", image.size, "white"), image)
    image = ImageOps.fit(image, (width, height), Image.Resampling.LANCZOS, centering=(.5, 0))
    ImageDraw.Draw(image).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, outline=edge)
    mask = Image.new("L", (width * 4, height * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width * 4 - 1, height * 4 - 1), radius=radius * 4, fill=255)
    image.putalpha(mask.resize((width, height), Image.Resampling.BOX))
    return image


def fit_scale(image, width, height):
    """The largest scale, up to 1:1, at which an image fits a width x height stage with its margin."""
    return min((max(width, 80) - 56) / image.width, (max(height, 80) - 56) / image.height, 1.0)


def ring(parent):
    """Focus-ring colour with at least 3:1 contrast against the background the control sits on."""
    red, green, blue = (value / 65535 for value in parent.winfo_rgb(parent.cget("bg")))
    return "#1f1e1b" if .2126 * red + .7152 * green + .0722 * blue > .5 else "#f3f1ea"


def entry(item):
    """History entry an item belongs to: its deck, or the image (or live run) itself."""
    return item.get("deck") or item["id"]


def is_run(item):
    """A live run rather than an image."""
    return bool(item) and item.get("kind") == "run"


def span(seconds):
    """A length of time in its two largest units at most."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
    return f"{seconds // 86400} d {seconds % 86400 // 3600} h"


def figure(value):
    """A number as short as it reads well: whole and large ones in groups of three digits, others to 4 figures."""
    if float(value).is_integer() or abs(value) >= 1000:
        return f"{value:,.0f}".replace(",", "\u202f")
    return f"{value:.4g}"


def ticks(low, high):
    """Three to five round values, evenly spaced, that span low..high."""
    rough = ((high - low) or abs(high) or 1) / 3
    unit = 10 ** math.floor(math.log10(rough))
    step = next(unit * factor for factor in (1, 2, 2.5, 5, 10) if unit * factor >= rough)
    first, last = math.floor(low / step), math.ceil(high / step)
    last += first == last
    return [index * step for index in range(first, last + 1)]


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
        self.loading = None  # the figure being read off the Tk thread, while there is one (see load_figure)
        self.scaled = None  # (view, image): the figure as last scaled for the stage
        self.thumbnails = {}
        self.fonts = {}
        self.conversations = {}
        self.hover = None
        families = set(tkfont.families(root))
        self.sans = "Inter" if "Inter" in families else "DejaVu Sans"
        self.serif = next((name for name in SERIFS if name in families), self.sans)
        self.fit = True
        self.scale = 1.0
        self.pan = [0, 0]
        self.pending_draw = None
        self.watch_busy = False
        self.watch_candidate = None
        self.follow = tk.BooleanVar(value=store.setting("follow", True))
        self.period = store.setting("period", "all")
        self.chat_open = store.setting("chat_open", False)
        self.sidebar_open = store.setting("sidebar_open", True)
        self.faces = {}
        self.chat_entry = None
        self.thread = None
        self.chat_key = None
        self.chat_drafts = {}
        self.point = None  # what the user marked to ask about: a point (x, y) or a region (left, top, right, bottom), in image pixels
        self.mark_from = None
        self.image_origin = (0, 0)
        self.runs = {}
        self.following = set()
        self.mirrors = {}  # other computers whose figures are shown here: {host: {"state": ...}}
        self.feeds = {}
        self.cards = {}
        self.run_scroll = 0
        self.run_height = 0
        self.run_plots = []
        self.pointer = None
        self.language = store.setting("language", "en")
        self.theme = store.setting("theme", "sand")
        use_theme(self.theme)
        root.title(self.t("window_title"))
        # Fixed sizes below are pixels at 96 dpi; they scale with the screen so HiDPI layouts keep their proportions.
        self.factor = max(1.0, round(float(root.tk.call("tk", "scaling")) / (96 / 72) * 4) / 4)
        root.geometry(store.setting("geometry", f"{self.px(1120)}x{self.px(760)}"))
        root.minsize(self.px(820), self.px(540))
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
        root.bind("<Key-s>", self.key(self.toggle_sidebar))
        root.bind("<Control-o>", lambda event: self.open_files())
        root.bind("<Control-plus>", lambda event: self.zoom(1.2))
        root.bind("<Control-equal>", lambda event: self.zoom(1.2))
        root.bind("<Control-minus>", lambda event: self.zoom(1 / 1.2))
        for run in store.runs(following=True):
            self.follow_run(run["id"])
        for host in store.setting("mirrors", []):
            self.follow_host(host)
        root.after(50, self.poll_events)
        root.after(900, self.watch)
        root.after(5000, self.tick)

    def px(self, value):
        return round(value * self.factor)

    def t(self, key, **values):
        return i18n.text(self.language, key, **values)

    def set_language(self, language):
        if language != self.language:
            self.language = language
            self.store.set_setting("language", language)
            self.rebuild()

    def set_theme(self, theme):
        if theme != self.theme:
            self.theme = theme
            self.store.set_setting("theme", theme)
            use_theme(theme)
            self.thumbnails = {}  # their outline is a theme colour
            self.rebuild()

    def rebuild(self):
        """Build the window again (another language or theme), keeping project, selection, filter and chat."""
        self.save_chat_draft()
        self.chat_key = None
        for child in self.root.winfo_children():
            child.destroy()
        self.root.configure(bg=BG)
        self.root.title(self.t("window_title"))
        self._build()
        self.apply_chat_layout()
        self.refresh_sessions()
        self.refresh_history()
        self.chat_entry = None
        if self.current:
            self.select(self.current)
        elif self.session:
            self.clear_view()
        else:
            self.load_chat()
            self.draw()

    def key(self, action):
        """Window shortcuts, except while the user is typing a question."""
        return lambda event: None if self.root.focus_get() in (self.chat_input, self.filter_entry) else action()

    def _label(self, parent, text, fg=None, bg=None, size=10, weight="normal", **kwargs):
        return tk.Label(parent, text=text, fg=fg or INK, bg=bg or parent.cget("bg"),
                        font=(self.sans, size, weight), **kwargs)

    def _button(self, parent, text, command, kind="plain", size=10, padx=13, pady=7, icon=None):
        """A rounded button. Tk only draws rectangles, so the shape is an image behind the text."""
        around = parent.cget("bg")
        # A 2 px ring in the parent's colour, which turns visible when the button has keyboard focus.
        button = tk.Button(parent, text=text, command=command, compound="center", relief="flat", bd=0, padx=0, pady=0,
                           bg=around, activebackground=around, highlightthickness=2, highlightbackground=around,
                           highlightcolor=ring(parent), cursor="hand2", font=(self.sans, size))
        button.look = (size, padx, pady, icon)
        button.bind("<Enter>", lambda event: self.skin(button, hot=True))
        button.bind("<Leave>", lambda event: self.skin(button))
        self.skin(button, kind)
        return button

    def skin(self, button, kind=None, hot=False):
        """Draw a button for its current text and look; call again when either changes."""
        button.kind = kind or button.kind
        size, padx, pady, icon = button.look
        fill, lit, ink = KINDS[button.kind]
        font = self.font(size)
        width = font.measure(button.cget("text")) + 2 * self.px(padx)
        height = font.metrics("linespace") + 2 * self.px(pady)
        key = (width, height, lit if hot and str(button.cget("state")) != "disabled" else fill, icon, ink)
        if key not in self.faces:
            image = Image.new("RGBA", (width * 4, height * 4), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            if key[2]:
                draw.rounded_rectangle((0, 0, width * 4 - 1, height * 4 - 1), radius=min(self.px(8), height // 2) * 4, fill=key[2])
            if icon == "sidebar":  # a window outline with its left pane marked
                unit = self.px(4)
                left, top, right, bottom = width * 2 - 9 * unit, height * 2 - 7 * unit, width * 2 + 9 * unit, height * 2 + 7 * unit
                draw.rounded_rectangle((left, top, right, bottom), radius=3 * unit, outline=ink, width=round(1.5 * unit))
                draw.line((left + 6 * unit, top, left + 6 * unit, bottom), fill=ink, width=round(1.5 * unit))
            elif icon:  # ("swatch", colour, chosen): a dot of that colour, ringed when it is the current theme
                unit, x, y = self.px(4), width * 2, height * 2
                draw.ellipse((x - 6 * unit, y - 6 * unit, x + 6 * unit, y + 6 * unit), fill=icon[1], outline=EDGE, width=unit // 2)
                if icon[2]:
                    draw.ellipse((x - 9.5 * unit, y - 9.5 * unit, x + 9.5 * unit, y + 9.5 * unit), outline=INK, width=round(1.5 * unit))
            self.faces[key] = ImageTk.PhotoImage(image.resize((width, height), Image.Resampling.BOX))
        button.configure(image=self.faces[key], fg=ink, activeforeground=ink)

    def font(self, size, weight="normal"):
        if (size, weight) not in self.fonts:
            self.fonts[size, weight] = tkfont.Font(root=self.root, family=self.sans, size=size, weight=weight)
        return self.fonts[size, weight]

    def round(self, frame, radius=8):
        """Round a frame: Tk frames are rectangles, so four small images in the colour around it cover its corners."""
        radius, around = self.px(radius), frame.master.cget("bg")
        key = ("corner", radius, around)
        if key not in self.faces:
            side = radius * 8
            mask = Image.new("L", (side, side), 255)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=radius * 4, fill=0)
            sheet = Image.new("RGBA", (side, side), around)
            sheet.putalpha(mask)
            sheet = sheet.resize((radius * 2, radius * 2), Image.Resampling.BOX)
            self.faces[key] = [ImageTk.PhotoImage(sheet.crop((x, y, x + radius, y + radius)))
                               for y in (0, radius) for x in (0, radius)]
        corners = [tk.Label(frame, image=image, bd=0, padx=0, pady=0, bg=frame.cget("bg")) for image in self.faces[key]]
        for corner, (x, y, anchor) in zip(corners, ((0, 0, "nw"), (1, 0, "ne"), (0, 1, "sw"), (1, 1, "se"))):
            corner.place(relx=x, rely=y, anchor=anchor)
        return corners

    def _chip(self, parent, text, command, chosen=False):
        """Small toggle-like button: filters, languages, history arrows."""
        return self._button(parent, text, command, "chosen" if chosen else "plain", size=8, padx=9, pady=4)

    def _build(self):
        self.browsing = False
        header = tk.Frame(self.root, bg=BG, height=self.px(54))
        header.pack(fill="x")
        header.pack_propagate(False)
        self.header_mark = ImageTk.PhotoImage(visor_mark(self.px(26), ACCENT, ON_ACCENT, BG))
        self.empty_mark = ImageTk.PhotoImage(visor_mark(self.px(56), EDGE, STAGE, STAGE))
        self._button(header, "", self.toggle_sidebar, "quiet", padx=17, icon="sidebar").pack(side="left", padx=(12, 8))
        tk.Label(header, image=self.header_mark, bg=BG, bd=0).pack(side="left", padx=(0, 9))
        self._label(header, "Visor", size=13, weight="bold").pack(side="left")
        # Right-hand group built left to right, so keyboard Tab moves in the order you read it.
        actions = tk.Frame(header, bg=BG)
        actions.pack(side="right", padx=(0, 14))
        for name in THEMES:  # one dot per theme; the current one is ringed
            self._button(actions, "", lambda value=name: self.set_theme(value), "quiet", padx=12, pady=6,
                         icon=("swatch", SWATCHES[name], name == self.theme)).pack(side="left")
        tk.Frame(actions, bg=BG, width=self.px(12)).pack(side="left")
        for code in i18n.LANGUAGES:
            self._chip(actions, code.upper(), lambda value=code: self.set_language(value), code == self.language).pack(side="left")
        self.ask_button = self._button(actions, self.t("ask"), self.toggle_chat)
        self.ask_button.pack(side="left", padx=(14, 0))
        # Agents send nearly everything; opening by hand is the secondary action, so it is quieter than Ask.
        self._button(actions, self.t("open_image"), self.open_files, "quiet").pack(side="left", padx=(4, 0))
        tk.Frame(self.root, height=1, bg=LINE).pack(fill="x")
        body = tk.Frame(self.root, bg=PANEL)
        body.pack(fill="both", expand=True)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Visor.Vertical.TScrollbar", background=EDGE, troughcolor=PANEL, bordercolor=PANEL,
                        lightcolor=EDGE, darkcolor=EDGE, arrowcolor=PANEL, gripcount=0, width=self.px(8))
        style.map("Visor.Vertical.TScrollbar", background=[("active", MUTED)])
        # The sidebar is either the history of one conversation or, from its top button, the list of all of them.
        # It closes from the header button (or S), leaving its width to the figure.
        self.sidebar = tk.Frame(body, bg=PANEL, width=self.px(SIDEBAR))
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        tk.Frame(self.sidebar, width=1, bg=LINE).pack(side="right", fill="y")
        self._build_browser(self.sidebar)
        sidebar = self.history_view = tk.Frame(self.sidebar, bg=PANEL)
        sidebar.pack(fill="both", expand=True)
        self._label(sidebar, self.t("project"), fg=MUTED, size=8, weight="bold").pack(anchor="w", padx=20, pady=(18, 8))
        picker = tk.Frame(sidebar, bg=CARD)
        picker.pack(fill="x", padx=12)
        self.session_button = tk.Button(picker, command=self.toggle_browser, bg=CARD, fg=INK, activebackground=CARD,
                                        activeforeground=INK, disabledforeground=MUTED, relief="flat", bd=0, anchor="w",
                                        highlightthickness=2, highlightbackground=CARD, highlightcolor=INK,
                                        cursor="hand2", font=(self.sans, 10, "bold"), padx=9, pady=5)
        self.session_button.pack(fill="x", padx=2, pady=2)
        self.round(picker)
        self.session_detail = self._label(sidebar, "", fg=MUTED, size=8, anchor="w")
        self.session_detail.pack(fill="x", padx=20, pady=(7, 0))
        tk.Checkbutton(sidebar, text=self.t("follow"), variable=self.follow, command=self.follow_changed,
            bg=PANEL, fg=INK, selectcolor=FIELD, activebackground=PANEL, activeforeground=INK,
            font=(self.sans, 9), highlightthickness=2, highlightbackground=PANEL, highlightcolor=INK, cursor="hand2").pack(anchor="w", padx=15, pady=(10, 12))
        tk.Frame(sidebar, height=1, bg=LINE).pack(fill="x", padx=12)
        history_title = tk.Frame(sidebar, bg=PANEL)
        history_title.pack(fill="x", padx=(20, 12), pady=(14, 8))
        self._label(history_title, self.t("history"), fg=MUTED, size=8, weight="bold").pack(side="left")
        # History moves with ↑ ↓ here; the arrows at the bottom of the figure are for slides only.
        self._chip(history_title, "↓", lambda: self.step(1)).pack(side="right")
        self._chip(history_title, "↑", lambda: self.step(-1)).pack(side="right", padx=(0, 2))
        self.count_label = self._label(history_title, "0", fg=MUTED, size=8)
        self.count_label.pack(side="right", padx=(0, 8))
        periods = tk.Frame(sidebar, bg=PANEL)
        periods.pack(fill="x", padx=12, pady=(0, 8))
        self.period_buttons = {}
        for key in PERIODS:
            button = self._chip(periods, self.t(f"period_{key}"), lambda value=key: self.set_period(value), key == self.period)
            button.pack(side="left", padx=(0, 2))
            self.period_buttons[key] = button
        self._label(sidebar, self.t("local"), fg=MUTED, size=7).pack(side="bottom", anchor="w", padx=20, pady=12)
        self.history_canvas, self.history_frame = self._scroll_list(sidebar)
        if not self.sidebar_open:
            self.sidebar.pack_forget()
        main = self.main = tk.Frame(body, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        self._build_chat(body)
        heading = tk.Frame(main, bg=BG)
        heading.pack(fill="x", padx=30, pady=(20, 16))
        # The title is set in a serif when the system has one; the rest of the window stays sans.
        self.title_label = tk.Label(heading, text=self.t("welcome_title"), fg=INK, bg=BG, anchor="w", justify="left",
                                    font=(self.serif, 20) if self.serif != self.sans else (self.sans, 17, "bold"),
                                    wraplength=self.px(650))
        self.title_label.pack(fill="x")
        self.caption_label = self._label(heading, self.t("welcome_caption"), fg=MUTED, size=10, anchor="w", justify="left", wraplength=self.px(650))
        self.caption_label.pack(fill="x", pady=(4, 0))
        self.new_button = self._button(main, self.t("new_images"), self.show_latest, "primary")
        # The stage runs edge to edge, so the figure gets all the width the window has.
        self.canvas = tk.Canvas(main, bg=STAGE, highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self.schedule_draw)
        self.canvas.bind("<Button-4>", lambda event: self.wheel(1.15, event))
        self.canvas.bind("<Button-5>", lambda event: self.wheel(1 / 1.15, event))
        self.canvas.bind("<MouseWheel>", lambda event: self.wheel(1.15 if event.delta > 0 else 1 / 1.15, event))
        self.canvas.bind("<Motion>", self.point_at)
        self.canvas.bind("<Leave>", lambda event: self.point_at(None))
        self.canvas.bind("<ButtonPress-1>", self.pan_start)
        self.canvas.bind("<B1-Motion>", self.pan_move)
        self.canvas.bind("<Double-Button-1>", lambda event: self.set_fit())
        self.canvas.bind("<Button-3>", self.mark)
        self.canvas.bind("<B3-Motion>", self.mark_drag)
        self.canvas.bind("<ButtonRelease-3>", self.mark_end)
        controls = tk.Frame(main, bg=BG)
        self.slide_bar = tk.Frame(controls, bg=BG)
        # The bottom row must fit beside the question panel at the narrowest window: compact buttons, labels sized to text.
        self._button(self.slide_bar, "‹", lambda: self.turn(-1), padx=10, pady=5).pack(side="left")
        self.slide_label = self._label(self.slide_bar, "", fg=INK, size=9, padx=8)
        self.slide_label.pack(side="left")
        self._button(self.slide_bar, "›", lambda: self.turn(1), padx=10, pady=5).pack(side="left")
        self.zoom_bar = tk.Frame(controls, bg=BG)
        self._button(self.zoom_bar, "−", lambda: self.zoom(1 / 1.2), padx=10, pady=5).pack(side="left")
        self.zoom_label = self._label(self.zoom_bar, "100 %", fg=INK, size=9, width=5)
        self.zoom_label.pack(side="left")
        self._button(self.zoom_bar, "+", lambda: self.zoom(1.2), padx=10, pady=5).pack(side="left")
        self._button(self.zoom_bar, "1:1", self.actual_size, padx=10, pady=5).pack(side="left", padx=4)
        self._button(self.zoom_bar, self.t("fit"), self.set_fit, padx=10, pady=5).pack(side="left")
        self.detail_label = self._label(main, self.t("idle_detail"), fg=MUTED, size=8, anchor="w")
        # Bottom rows are laid out before the canvas, so a short window shrinks the figure, not them.
        self.detail_label.pack(side="bottom", fill="x", padx=30, pady=(0, 12), before=self.canvas)
        controls.pack(side="bottom", fill="x", padx=26, pady=(10, 6), before=self.canvas)
        self.show_all_button = self._button(self.canvas, self.t("show_all"), lambda: self.set_period("all"))

    def _scroll_list(self, parent):
        """A vertical list that scrolls: returns its canvas and the frame the rows go in."""
        area = tk.Frame(parent, bg=PANEL)
        area.pack(fill="both", expand=True)
        canvas = tk.Canvas(area, bg=PANEL, highlightthickness=0, bd=0, width=self.px(SIDEBAR - 12))
        canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(area, orient="vertical", command=canvas.yview, style="Visor.Vertical.TScrollbar")
        scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scroll.set)
        frame = tk.Frame(canvas, bg=PANEL)
        window = canvas.create_window(0, 0, window=frame, anchor="nw")
        frame.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind("<Leave>", lambda event: self.hover_row(None))
        # Rows answer through one binding tag per list: binding every widget of every row made long lists slow to build.
        canvas.rows = f"Rows{canvas}"

        def row(event):
            widget = event.widget
            while not hasattr(widget, "command"):
                widget = widget.master
            return widget
        canvas.bind_class(canvas.rows, "<Button-1>", lambda event: row(event).command())
        canvas.bind_class(canvas.rows, "<Enter>", lambda event: self.hover_row(row(event)))
        canvas.bind_class(canvas.rows, "<Button-4>", lambda event: canvas.yview_scroll(-3, "units"))
        canvas.bind_class(canvas.rows, "<Button-5>", lambda event: canvas.yview_scroll(3, "units"))
        return canvas, frame

    def _row(self, canvas, frame, image, title, meta, command, selected=False, indent=0, tags="", tags_fg=None):
        """One clickable row of a sidebar list: thumbnail, title and a quieter second line."""
        row = tk.Frame(frame, bg=PANEL, cursor="hand2")
        row.pack(fill="x", padx=(8 + indent, 4), pady=1)
        row.rounded = False
        if image:
            row.icon = tk.Label(row, image=image, bg=PANEL, bd=0)
            row.icon.pack(side="left", padx=(8, 10), pady=7, anchor="n")
        else:
            self._label(row, "◇", fg=ACCENT, size=16).pack(side="left", padx=(8, 10), pady=7, anchor="n")
        text = tk.Frame(row, bg=PANEL)
        text.pack(side="left", fill="x", expand=True, pady=6)
        room = self.px(SIDEBAR - 52 - indent) - (image.width() if image else self.px(20))
        self._label(text, title, size=9, weight="bold", anchor="w", wraplength=room, justify="left").pack(anchor="w")
        row.meta = self._label(text, meta, fg=MUTED, size=8)
        row.meta.pack(anchor="w", pady=(2, 0))
        if tags:
            self._label(text, tags, fg=tags_fg or MUTED, size=7).pack(anchor="w", pady=(3, 0))

        row.command = command

        def tag_tree(widget):
            widget.bindtags((canvas.rows, *widget.bindtags()))
            for child in widget.winfo_children():
                tag_tree(child)
        tag_tree(row)
        self.paint_row(row, selected)
        return row

    def paint_row(self, row, selected=None):
        """The selected row is filled; the row under the pointer (or the keyboard) is slightly lit."""
        if selected is not None:
            row.selected = selected
        color = SELECTED if row.selected else (CARD if row is self.hover else PANEL)
        if color != PANEL and not row.rounded:  # corners only for rows that ever show a fill
            row.rounded = True
            self.round(row)
        self._recolor(row, color)

    def hover_row(self, row):
        previous, self.hover = self.hover, row
        for changed in (previous, row):
            if changed is not None and changed.winfo_exists():
                self.paint_row(changed)

    def thumbnail(self, snapshot, width, height):
        key = (snapshot, width, height)
        if key not in self.thumbnails:
            small = Path(snapshot).with_suffix(".thumb.png")
            try:
                self.thumbnails[key] = ImageTk.PhotoImage(tile(small if small.exists() else snapshot, width, height, self.px(4), EDGE))
            except (OSError, ValueError):
                self.thumbnails[key] = None
        return self.thumbnails[key]

    def clip(self, text, width, size=10, weight="normal", keep_end=False):
        """Shorten a text with an ellipsis so it fits in width pixels."""
        measure = self.font(size, weight).measure
        if measure(text) <= width:
            return text
        while text and measure(text + "…") > width:
            text = text[1:] if keep_end else text[:-1]
        return "…" + text if keep_end else text + "…"

    def day(self, moment):
        date, today = datetime.fromtimestamp(moment).date(), datetime.now().date()
        if (today - date).days in (0, 1):
            return self.t("period_today") if date == today else self.t("yesterday")
        return date.strftime("%d %b" if date.year == today.year else "%d %b %Y")

    def ago(self, moment):
        minutes = int((time.time() - moment) / 60)
        if minutes < 1:
            return self.t("ago_now")
        if minutes < 60:
            return self.t("ago_min", n=minutes)
        return self.t("ago_hour", n=minutes // 60) if minutes < 24 * 60 else self.day(moment)

    def _build_browser(self, outer):
        view = self.browser_view = tk.Frame(outer, bg=PANEL)
        top = tk.Frame(view, bg=PANEL)
        top.pack(fill="x", padx=20, pady=(16, 10))
        self.browser_title = self._label(top, self.t("conversations"), fg=MUTED, size=8, weight="bold")
        self.browser_title.pack(side="left")
        self._chip(top, self.t("back"), self.toggle_browser).pack(side="right")
        self.filter_var = tk.StringVar()
        field = tk.Frame(view, bg=FIELD)
        field.pack(fill="x", padx=12)
        self.filter_entry = tk.Entry(field, textvariable=self.filter_var, bg=FIELD, fg=INK, insertbackground=INK,
                                     relief="flat", bd=0, highlightthickness=0, font=(self.sans, 10))
        self.filter_entry.pack(fill="x", padx=10, pady=7)
        self.round(field)
        self._label(view, self.t("filter_hint"), fg=MUTED, size=7, anchor="w").pack(fill="x", padx=20, pady=(6, 10))
        self.filter_var.trace_add("write", lambda *_: self.fill_browser())
        self.filter_entry.bind("<Up>", lambda event: self.move_browser(-1))
        self.filter_entry.bind("<Down>", lambda event: self.move_browser(1))
        self.filter_entry.bind("<Return>", lambda event: self.pick_browser())
        self.filter_entry.bind("<Escape>", lambda event: self.toggle_browser())
        tk.Frame(view, height=1, bg=LINE).pack(fill="x", padx=12)
        self.browser_canvas, self.browser_frame = self._scroll_list(view)
        self.browser_rows = []

    def toggle_browser(self):
        """Swap the sidebar between the history of one conversation and the list of all of them."""
        self.browsing = not self.browsing
        if self.browsing:
            self.history_view.pack_forget()
            self.browser_view.pack(fill="both", expand=True)
            self.filter_var.set("")  # also fills the list
            self.filter_entry.focus_set()
        else:
            self.browser_view.pack_forget()
            self.history_view.pack(fill="both", expand=True)
            self.canvas.focus_set()

    def fill_browser(self):
        """Conversations grouped by project, most recently used first; the filter matches any of their names."""
        for child in self.browser_frame.winfo_children():
            child.destroy()
        self.browser_rows, self.hover = [], None
        words = self.filter_var.get().lower().split()
        groups = {}
        for row in self.sessions:
            if all(word in f"{row['session']} {row['project']} {row['label']}".lower() for word in words):
                groups.setdefault(row["project"], []).append(row)
        self.browser_title.configure(text=f"{self.t('conversations')}   {sum(map(len, groups.values()))}")
        for project, rows in groups.items():
            grouped = len(rows) > 1
            if grouped:
                self._label(self.browser_frame, self.clip(project, self.px(SIDEBAR - 50), 8, "bold"), fg=MUTED, size=8,
                            weight="bold", anchor="w").pack(fill="x", padx=16, pady=(14, 4))
            for row in rows:
                name = row["label"] or self.t("main_folder") if grouped else "  ·  ".join(filter(None, [project, row["label"]]))
                figures = self.t("figures_one") if row["entries"] == 1 else self.t("figures_many", n=row["entries"])
                widget = self._row(self.browser_canvas, self.browser_frame,
                                   row["snapshot"] and self.thumbnail(row["snapshot"], self.px(44), self.px(32)),
                                   name, f"{figures}  ·  {self.ago(row['updated'])}",
                                   lambda value=row["session"]: self.pick_browser(value),
                                   selected=row["session"] == self.session, indent=8 if grouped else 0)
                self.browser_rows.append((row["session"], widget))
        if not self.browser_rows:
            self._label(self.browser_frame, self.t("no_match") if self.sessions else self.t("no_projects"),
                        fg=MUTED, size=9).pack(anchor="w", padx=20, pady=12)
        # The keyboard starts on the conversation being shown, or on the best match while filtering.
        shown = [index for index, (session, _) in enumerate(self.browser_rows) if session == self.session]
        self.browser_index = shown[0] if shown and not words else 0
        self.move_browser(0)

    def move_browser(self, direction):
        if self.browser_rows:
            self.browser_index = max(0, min(len(self.browser_rows) - 1, self.browser_index + direction))
            self.hover_row(self.browser_rows[self.browser_index][1])
            if direction:
                self.browser_canvas.yview_moveto(max(0, self.browser_index - 2) / len(self.browser_rows))
        return "break"

    def pick_browser(self, session=None):
        if session is None and self.browser_rows:
            session = self.browser_rows[self.browser_index][0]
        if session is not None:
            self.toggle_browser()
            self.select_session(session)
        return "break"

    def _build_chat(self, body):
        self.chat = tk.Frame(body, bg=PANEL, width=self.px(380))
        self.chat.pack_propagate(False)
        tk.Frame(self.chat, width=1, bg=LINE).pack(side="left", fill="y")
        top = tk.Frame(self.chat, bg=PANEL)
        top.pack(fill="x", padx=(18, 8), pady=(12, 2))
        self._label(top, self.t("chat_title"), fg=MUTED, size=8, weight="bold").pack(side="left")
        self._button(top, "✕", self.toggle_chat, "quiet", size=9, padx=8, pady=4).pack(side="right")
        self.chat_status = self._label(self.chat, "", fg=MUTED, size=8, anchor="w", justify="left", wraplength=self.px(340))
        self.chat_status.pack(fill="x", padx=18)
        self.chat_modes = tk.Frame(self.chat, bg=PANEL)
        self.mode_buttons = {}
        for mode, label in (("figure", "figure_chat"), ("live", "original_chat")):
            button = self._button(self.chat_modes, self.t(label), lambda value=mode: self.set_chat_mode(value),
                                  size=9, padx=12, pady=6)
            button.pack(side="left", padx=(0, 5))
            self.mode_buttons[mode] = button
        actions = self.chat_actions = tk.Frame(self.chat, bg=PANEL)
        self.thread_button = self._button(actions, self.t("new_thread"), self.new_thread, size=9, padx=11, pady=5)
        self.thread_button.pack(side="left")
        self.copy_button = self._button(actions, self.t("copy_command"), self.copy_command, size=9, padx=11, pady=5)
        self.copy_button.pack(side="left", padx=4)
        bottom = self.chat_bottom = tk.Frame(self.chat, bg=PANEL)
        self.working_label = self._label(bottom, self.t("working"), fg=ACCENT_TEXT, size=8, anchor="w")
        self.point_bar = tk.Frame(bottom, bg=PANEL)
        self.point_label = self._label(self.point_bar, "", fg=ACCENT_TEXT, size=8, anchor="w")
        self.point_label.pack(side="left")
        tk.Button(self.point_bar, text=self.t("remove"), command=self.clear_point, bg=PANEL, fg=MUTED, activebackground=PANEL,
                  activeforeground=INK, relief="flat", bd=0, highlightthickness=2, highlightbackground=PANEL,
                  highlightcolor=INK, cursor="hand2",
                  font=(self.sans, 8, "underline")).pack(side="right")
        # The question box: a white rounded sheet holding the text and its Send button. Its focus ring is the
        # sheet's own rounded edge (composer_ring), not a rectangle around the text inside it.
        self.composer_ring = tk.Frame(bottom, bg=FIELD)
        self.composer_ring.pack(fill="x")
        composer = self.composer = tk.Frame(self.composer_ring, bg=FIELD)
        composer.pack(fill="x", padx=self.px(2), pady=self.px(2))
        self.chat_input = tk.Text(composer, height=3, wrap="word", bg=FIELD, fg=INK, insertbackground=INK, relief="flat",
                                  font=(self.sans, 10), padx=6, pady=4, highlightthickness=0)
        self.chat_input.pack(fill="x", padx=6, pady=(6, 0))
        self.chat_input.bind("<Return>", self.chat_return)
        self.chat_input.bind("<FocusIn>", lambda event: self.ring_composer(ACCENT))
        self.chat_input.bind("<FocusOut>", lambda event: self.ring_composer(FIELD))
        self.send_button = self._button(composer, self.t("send"), self.send_or_stop, "primary", size=9, padx=13, pady=5)
        self.send_button.pack(anchor="e", padx=8, pady=8)
        self.composer_corners = self.round(self.composer_ring, 13), self.round(composer, 11)
        for corner in self.composer_corners[0]:
            corner.lower()  # under the sheet, so only the edge shows
        self.chat_log = tk.Text(self.chat, wrap="word", bg=PANEL, fg=INK, relief="flat", font=(self.sans, 10),
                                padx=2, pady=4, state="disabled", highlightthickness=0, cursor="arrow", takefocus=0,
                                spacing2=2)
        # A question is a bubble on the right; the answer under it is plain text, so turns are easy to tell apart.
        self.chat_log.tag_configure("user", justify="right")
        self.chat_log.tag_configure("gap", font=(self.sans, 4))
        self.chat_log.tag_configure("assistant", lmargin1=self.px(2), lmargin2=self.px(2))
        self.chat_log.tag_configure("tool", foreground=MUTED, font=(self.sans, 8))
        self.chat_log.tag_configure("error", foreground=ERROR)
        self.chat_log.tag_configure("note", foreground=MUTED, font=(self.sans, 9, "italic"))
        self.chat_log.tag_configure("bold", font=(self.sans, 10, "bold"))
        self.chat_log.tag_configure("code", font=("DejaVu Sans Mono", 9))

    def ring_composer(self, color):
        outer, inner = self.composer_corners
        self.composer_ring.configure(bg=color)
        for corner in outer:
            corner.configure(bg=color)
        for corner in inner:  # their solid part is the ring's colour, so they are drawn again
            corner.destroy()
        self.composer_corners = outer, self.round(self.composer, 11)

    def show_chat_controls(self, visible):
        """Without a session to ask, the panel shows only its explanation."""
        for widget in (self.chat_modes, self.chat_actions, self.chat_bottom, self.chat_log):
            widget.pack_forget()
        if visible:
            if self.current.get("agent_provider") == "codex":
                self.chat_modes.pack(fill="x", padx=16, pady=(12, 0), before=self.chat_status)
            self.chat_actions.pack(fill="x", padx=16, pady=(10, 8))
            self.chat_bottom.pack(side="bottom", fill="x", padx=16, pady=(8, 16))
            self.chat_log.pack(fill="both", expand=True, padx=16)

    def apply_chat_layout(self):
        if self.chat_open:
            # Never ask for more than the screen has.
            needed = min(self.px(CHAT_MIN_WINDOW), self.root.winfo_screenwidth())
            self.root.minsize(needed, self.px(540))
            self.root.update_idletasks()
            width, height = self.root.winfo_width(), self.root.winfo_height()
            if width < needed:
                self.root.geometry(f"{needed}x{max(self.px(540), height)}")
            self.set_chat_width(self.chat_width(max(width, needed)))
            self.chat.pack(side="right", fill="y", before=self.main)
        else:
            self.root.minsize(self.px(820), self.px(540))
            self.chat.pack_forget()
        self.skin(self.ask_button, "chosen" if self.chat_open else "plain")

    def chat_width(self, window_width):
        return max(self.px(300), min(self.px(380), int(window_width * .3)))

    def resize_chat(self, event):
        if event.widget is self.root and self.chat_open:
            width = self.chat_width(event.width)
            if int(self.chat.cget("width")) != width:
                self.set_chat_width(width)

    def set_chat_width(self, width):
        # The status text and the question bubbles wrap inside the panel, whatever width the panel has.
        self.chat.configure(width=width)
        self.chat_status.configure(wraplength=width - self.px(40))
        for bubble in self.chat_log.winfo_children():
            bubble.winfo_children()[0].configure(wraplength=width - self.px(96))

    def _icon(self):
        # Several sizes, so the window manager scales down from a sharp one instead of up from 64 px.
        self.window_icons = [ImageTk.PhotoImage(visor_mark(size, *ICON)) for size in (256, 128, 64, 48, 32, 16)]
        self.root.iconphoto(True, *self.window_icons)

    def refresh_sessions(self):
        self.sessions = self.store.sessions()
        for row in self.sessions:
            if row["session"] not in self.conversations:  # reading .git once per conversation is enough
                self.conversations[row["session"]] = conversation(row["session"])
            row["project"], row["label"] = self.conversations[row["session"]]
        current = next((row for row in self.sessions if row["session"] == self.session), None)
        name = current["project"] if current else self.t("no_projects")
        self.session_button.configure(text=self.clip(name, self.px(SIDEBAR - 80), 10, "bold") + "   ▾",
                                      state="normal" if self.sessions else "disabled")
        # Under the name: which conversation of the project, and for a folder, where it is.
        detail = ""
        if current:
            folder = current["session"].replace(str(Path.home()), "~", 1) if current["session"].startswith("/") else ""
            detail = self.clip("  ·  ".join(filter(None, [current["label"], folder])), self.px(SIDEBAR - 42), 8, keep_end=True)
        self.session_detail.configure(text=detail)
        if self.browsing:
            self.fill_browser()

    def select_session(self, session):
        self.session = session
        self.store.set_setting("session", session)
        self.refresh_sessions()
        self.refresh_history()
        if self.items:
            self.select(self.items[0])
        elif self.current:
            self.clear_view()  # nothing of this project in the chosen period
        self.new_button.pack_forget()

    def clear_view(self):
        """Nothing of this project falls in the chosen period: show an empty canvas, not another project's image."""
        if self.original:
            self.original.close()
        self.current, self.original, self.pages, self.point, self.loading = None, None, [], None, None
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
        if not self.current or not any(entry(item) == entry(self.current) for item in self.items):
            if self.items:
                self.select(self.items[0])
            else:
                self.clear_view()

    def paint_periods(self):
        for key, button in self.period_buttons.items():
            self.skin(button, "chosen" if key == self.period else "plain")

    def refresh_history(self):
        self.items = []
        if self.session:  # images and live runs share one list, newest first
            self.items = sorted(self.store.history(self.session, 200, collapse=True, since=self.since())
                                + self.store.runs(self.session, self.since()), key=lambda item: item["created"], reverse=True)[:200]
        self.update_position()
        for child in self.history_frame.winfo_children():
            child.destroy()
        self.cards, self.hover = {}, None
        versions = self.store.versions(self.session) if self.session else {}
        heading = None
        for index, item in enumerate(self.items):
            if self.day(item["created"]) != heading:  # one heading per day keeps a long history easy to scan
                heading = self.day(item["created"])
                self._label(self.history_frame, heading, fg=MUTED, size=8, weight="bold", anchor="w").pack(
                    fill="x", padx=16, pady=(12 if index else 4, 4))
            parts = [self.t("latest")] if index == 0 else []
            if is_run(item):
                if not item["closed"]:
                    parts.append(self.t("live"))
                self.cards[item["id"]] = self._row(
                    self.history_canvas, self.history_frame, self.ring_icon(item), item["title"], self.run_line(item),
                    lambda value=item: self.select(value), selected=bool(self.current and item["id"] == entry(self.current)),
                    tags="  ·  ".join(parts), tags_fg=ACCENT_TEXT if index == 0 else MUTED)
                continue
            number, total = versions.get(entry(item), (1, 1))
            if total > 1:  # a version only when the same file was shown more than once
                parts.append(self.t("version", n=number))
            if item.get("deck"):
                parts.append(self.t("slides_one") if item["pages"] == 1 else self.t("slides_many", n=item["pages"]))
            self.cards[entry(item)] = self._row(
                self.history_canvas, self.history_frame, self.thumbnail(item["snapshot"], self.px(64), self.px(46)),
                item["title"], datetime.fromtimestamp(item["created"]).strftime("%H:%M"), lambda value=item: self.select(value),
                selected=bool(self.current and entry(item) == entry(self.current)),
                tags="  ·  ".join(parts), tags_fg=ACCENT_TEXT if index == 0 else MUTED)
        if self.session and not self.items:
            self._label(self.history_frame, self.t("nothing_history"), fg=MUTED, size=9).pack(anchor="w", padx=14, pady=6)

    def update_position(self):
        """'k / n' in the history header when the shown entry is in the list, else just n."""
        index = next((i for i, value in enumerate(self.items) if self.current and entry(value) == entry(self.current)), None)
        self.count_label.configure(text=str(len(self.items)) if index is None else f"{len(self.items) - index} / {len(self.items)}")

    def select(self, item):
        if self.original:
            self.original.close()
        self.current = item
        self.point = None
        self.show_point()
        run = is_run(item)
        if run or not item.get("deck"):
            self.pages = []
        elif not self.pages or self.pages[0]["deck"] != item["deck"]:
            self.pages = self.store.deck_pages(item["deck"])
        self.original = self.loading = None
        if not run:
            self.load_figure(item)
        self.fit = True
        self.pan = [0, 0]
        self.run_scroll = 0
        self.watch_candidate = None
        self.canvas.configure(cursor="arrow" if run else "fleur")
        self.title_label.configure(text=item["title"])
        self.root.title(f"{item['title']} · Visor")
        source = Path(item["source"])
        if run:
            self.caption_label.configure(text=item["source"].replace(str(Path.home()), "~", 1))
            self.detail_label.configure(text=self.t("run_detail"))
        else:
            self.caption_label.configure(text=item["caption"] or source.name)
            # File name before folder, so a long path truncates the folder and not the name.
            self.detail_label.configure(text=f"{item['width']} × {item['height']} px   ·   {source.name}   ·   {source.parent}")
        if self.pages:
            self.slide_label.configure(text=self.t("slide", page=item["page"], pages=len(self.pages)))
            self.slide_bar.pack(side="left")
        else:
            self.slide_bar.pack_forget()
        for key, card in self.cards.items():
            self.paint_row(card, key == entry(item))
        self.update_position()
        if self.chat_entry != entry(item):
            self.load_chat()
        self.draw()

    def load_figure(self, item):
        """Read the figure and scale it to the stage off the Tk thread: the window answers the click at once
        and the figure follows. Until it does, the stage keeps what it was showing."""
        ticket = self.loading = [None]  # the worker leaves its result here, so one overtaken by a later choice is just dropped
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()

        def worker():
            original = scaled = None
            try:
                with Image.open(item["snapshot"]) as image:
                    # Flattened on the white sheet it is drawn over: an opaque image is quicker to scale, a large one much quicker.
                    image = image.convert("RGBA")
                    original = Image.new("RGB", image.size, "white")
                    original.paste(image, mask=image)
                scale = fit_scale(original, width, height)
                size = (max(1, round(original.width * scale)), max(1, round(original.height * scale)))
                scaled = ((0, 0, *original.size), size), original.resize(size, Image.Resampling.LANCZOS, reducing_gap=2.0)
            except (OSError, ValueError):
                pass
            finally:
                ticket[0] = (original, scaled)
        threading.Thread(target=worker, daemon=True).start()
        self.root.after(3, self.take_figure, ticket)

    def take_figure(self, ticket):
        if ticket is not self.loading:
            return  # another entry was chosen meanwhile
        if ticket[0] is None:
            self.root.after(3, self.take_figure, ticket)
            return
        self.loading, (self.original, self.scaled) = None, ticket[0]
        self.draw()

    def _recolor(self, widget, color):
        widget.configure(bg=color)
        for child in widget.winfo_children():
            self._recolor(child, color)

    def schedule_draw(self, event=None):
        if self.pending_draw:
            self.root.after_cancel(self.pending_draw)
        self.pending_draw = self.root.after(45, self.draw)
        self.caption_label.configure(wraplength=max(self.px(300), self.canvas.winfo_width() - self.px(60)))
        self.title_label.configure(wraplength=max(self.px(240), self.canvas.winfo_width() - self.px(60)))

    def draw(self):
        self.pending_draw = None
        if self.loading:
            return  # the figure being read is drawn when it arrives
        canvas = self.canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if is_run(self.current):
            self.zoom_bar.pack_forget()
            self.draw_run()
            return
        if not self.original:
            self.zoom_bar.pack_forget()
            if self.session and self.period != "all":
                # A filter hides everything: offer the way back instead of the first-launch welcome.
                canvas.create_window(width / 2, height / 2, window=self.show_all_button)
                return
            p = self.px
            canvas.create_image(width / 2, height / 2 - p(42), image=self.empty_mark)
            canvas.create_text(width / 2, height / 2 + p(10), text=self.t("empty_canvas"), fill=INK, font=(self.serif, 17))
            canvas.create_text(width / 2, height / 2 + p(46), text=self.t("empty_canvas_hint"), fill=MUTED, font=(self.sans, 10))
            return
        if not self.zoom_bar.winfo_manager():
            self.zoom_bar.pack(side="right", padx=(16, 0))
        if self.fit:
            self.scale = fit_scale(self.original, width, height)
            self.pan = [0, 0]
        # Render only the visible crop: zooming a large image never allocates a giant bitmap.
        scaled_w, scaled_h = self.original.width * self.scale, self.original.height * self.scale
        left = (width - scaled_w) / 2 + self.pan[0]
        top = (height - scaled_h) / 2 + self.pan[1]
        self.image_origin = (left, top)
        for spread, shade in zip((8, 5, 2), SHADOWS):  # a soft shadow under the sheet
            canvas.create_rectangle(left - spread, top - spread + 4, left + scaled_w + spread, top + scaled_h + spread + 4,
                                    fill=shade, outline="")
        canvas.create_rectangle(left, top, left + scaled_w, top + scaled_h, fill="white", outline="")
        x0 = max(0, -left / self.scale)
        y0 = max(0, -top / self.scale)
        x1 = min(self.original.width, (width - left) / self.scale)
        y1 = min(self.original.height, (height - top) / self.scale)
        if x1 > x0 and y1 > y0:
            box = (int(x0), int(y0), min(self.original.width, int(x1) + 1), min(self.original.height, int(y1) + 1))
            size = (max(1, round((box[2] - box[0]) * self.scale)), max(1, round((box[3] - box[1]) * self.scale)))
            if not self.scaled or self.scaled[0] != (box, size):  # the same view again (a marker, a repaint) is not scaled again
                self.scaled = (box, size), self.original.crop(box).resize(size, Image.Resampling.LANCZOS, reducing_gap=2.0)
            self.photo = ImageTk.PhotoImage(self.scaled[1])
            canvas.create_image(left + box[0] * self.scale, top + box[1] * self.scale, image=self.photo, anchor="nw")
        self.draw_marker()
        self.zoom_label.configure(text=f"{round(self.scale * 100)} %")

    def draw_marker(self):
        """What the user marked, over the figure: a ring on a point, a frame around a region."""
        canvas, (left, top) = self.canvas, self.image_origin
        canvas.delete("marker")
        if not self.point:
            return
        spots = [(left + x * self.scale, top + y * self.scale) for x, y in zip(self.point[::2], self.point[1::2])]
        if len(spots) == 1:
            r = self.px(11)
            box, shape = (spots[0][0] - r, spots[0][1] - r, spots[0][0] + r, spots[0][1] + r), canvas.create_oval
        else:
            box, shape = (*spots[0], *spots[1]), canvas.create_rectangle
        shape(*box, outline="white", width=self.px(6), tags="marker")
        shape(*box, outline="#e5484d", width=self.px(3), tags="marker")

    def clock(self, moment):
        date = datetime.fromtimestamp(moment)
        return date.strftime("%H:%M" if date.date() == datetime.now().date() else "%d %b %H:%M")

    def run_line(self, run):
        """Second line of a run's row in the history: how far it is and how long is left."""
        data, state = run["data"], runs.state(run)
        if state == "done":
            return f"{self.t('run_done')}  ·  {self.clock(run['updated'])}"
        if data["step"] is None:
            return self.t("run_running" if state == "running" else f"run_row_{state}")
        parts = [f"{100 * data['step'] / data['total']:.0f} %" if data["total"] else self.t("run_step", step=figure(data["step"]))]
        left = runs.remaining(data)
        if state != "running":
            parts.append(self.t(f"run_row_{state}").lower())
        elif left is not None:
            parts.append(span(left))
        return "  ·  ".join(parts)

    def ring_icon(self, run):
        """What a run shows where an image shows its thumbnail: a ring, filled as far as the run has got."""
        data = run["data"]
        share = round(60 * min(1, data["step"] / data["total"])) if data["step"] is not None and data["total"] else 0
        color = ACCENT if runs.state(run) in ("running", "waiting", "done") else MUTED
        key = ("ring", share, color, EDGE, self.factor)
        if key not in self.faces:
            width, height, radius, stroke = (self.px(value) * 4 for value in (64, 46, 13, 4))
            image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            box = (width / 2 - radius, height / 2 - radius, width / 2 + radius, height / 2 + radius)
            draw = ImageDraw.Draw(image)
            draw.ellipse(box, outline=EDGE, width=stroke)
            if share:
                draw.arc(box, -90, -90 + 6 * share, fill=color, width=stroke)
            self.faces[key] = ImageTk.PhotoImage(image.resize((width // 4, height // 4), Image.Resampling.BOX))
        return self.faces[key]

    def paint_run_row(self, run):
        row = self.cards.get(run["id"])
        if row is not None and row.winfo_exists():
            row.icon.configure(image=self.ring_icon(run))
            row.meta.configure(text=self.run_line(run))

    def follow_run(self, run_id):
        """Start the thread that reads what a run's file gains, unless one is already doing it."""
        if run_id not in self.following:
            self.following.add(run_id)
            threading.Thread(target=self.run_worker, args=(run_id,), daemon=True).start()

    def run_worker(self, started):
        """Off the Tk thread: the window only hears that a run has something new."""
        run_id, failed = started, True
        try:
            while True:
                run, changed = runs.advance(self.store, run_id)
                if run is None:
                    break
                run_id = run["id"]
                if changed:
                    self.events.put(({"action": "run", "id": run_id}, None, None))
                if run["closed"]:
                    break
                # A file on another machine costs an ssh connection per look; one that cannot be read is retried slowly.
                time.sleep(60 if run["error"] else 10 if runs.REMOTE.match(run["source"]) else 2)
            failed = False
        finally:
            self.events.put(({"action": "run_end", "id": run_id, "started": started, "failed": failed}, None, None))

    def follow_host(self, host, on=True):
        """Show here what agents show on another computer (see mirror.py). It is remembered until turned off."""
        hosts = [name for name in self.store.setting("mirrors", []) if name != host] + [host] * on
        self.store.set_setting("mirrors", hosts)
        if on and self.mirrors.get(host, {"paused": True}).get("paused"):  # not followed yet, or waiting to be asked again
            link = self.mirrors[host] = {"state": "connecting"}
            runs.SHARED[host] = mirror.sharing(host)
            threading.Thread(target=self.mirror_worker, args=(host, link), daemon=True).start()
        elif not on and host in self.mirrors:
            del self.mirrors[host]
            runs.SHARED.pop(host, None)
            if host in self.feeds:
                self.feeds[host].terminate()

    def mirror_worker(self, host, link):
        """Off the Tk thread: keeps the connection to the other computer and stores what it sends; the window
        only hears of entries already stored. A lost connection is tried again, and continues where it stopped.
        What ssh has to ask, it asks in a dialog; when the answer does not connect, it waits to be told to try again."""
        ask = dict(os.environ, SSH_ASKPASS=mirror.askpass(), SSH_ASKPASS_REQUIRE="force")
        while self.mirrors.get(host) is link:
            cursor = self.store.setting(f"mirror:{host}", {})
            connected = False
            with tempfile.TemporaryFile() as errors:
                try:
                    process = self.feeds[host] = subprocess.Popen(mirror.command(host, cursor, runs.SHARED.get(host, ())),
                                                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                                                 text=True, env=ask)
                    for line in process.stdout:
                        connected = True
                        link["state"] = "connected"
                        if not line.strip():
                            continue
                        try:
                            action, items, at = mirror.receive(self.store, host, line)
                        except (ValueError, KeyError, TypeError, OSError):
                            traceback.print_exc()
                            continue
                        cursor.update(at)
                        self.store.set_setting(f"mirror:{host}", cursor)
                        if items and self.mirrors.get(host) is link:
                            self.events.put(({"action": action, "items": items}, None, None))
                    process.stdout.close()
                    process.wait()
                    errors.seek(0)
                    said = errors.read().decode("utf-8", "replace").strip().splitlines()
                    detail = [line for line in said if line != mirror.ASKED]
                    link["state"] = detail[-1][:200] if detail else "connection closed"
                    if mirror.ASKED in said and not connected:  # asking again every few seconds would only pester
                        link["paused"] = True
                        link["state"] += f" (run `visor follow {host}` to try again)"
                        return
                except OSError as exc:
                    link["state"] = str(exc)
            again = time.monotonic() + mirror.RETRY
            while self.mirrors.get(host) is link and time.monotonic() < again:
                time.sleep(.2)

    def tick(self):
        """The time left and the time since the last line move on their own between two looks at the file."""
        try:
            for item in self.items:
                if is_run(item) and not item["closed"]:
                    self.paint_run_row(item)
            if is_run(self.current) and not self.current["closed"]:
                self.draw()
        finally:
            self.root.after(5000, self.tick)

    def draw_run(self):
        """The page of a live run: how far it is and how long is left, one chart per metric, the agent's notes.
        Tk's own lines are not antialiased, so every shape is painted on one image at 3x and reduced; the text is Tk's."""
        canvas, p, run = self.canvas, self.px, self.current
        data, state, now = run["data"], runs.state(run), time.time()
        width, height = max(canvas.winfo_width(), 80), max(canvas.winfo_height(), 80)
        over = 2 if self.factor > 1.25 else 3
        sheet = Image.new("RGB", (width * over, height * over), STAGE)
        pen = ImageDraw.Draw(sheet)
        marks = []  # painted at the end: cards first (their height depends on the text inside), marks over them

        def box(x0, y0, x1, y1, fill, radius):
            if x1 > x0 and y1 > y0:
                pen.rounded_rectangle((x0 * over, y0 * over, x1 * over, y1 * over), radius=min(radius, (y1 - y0) / 2) * over, fill=fill)

        def card(x0, y0, x1, y1):
            marks.insert(0, lambda: box(x0, y0, x1, y1, FIELD, p(14)))

        def text(x, y, value, size=10, fill=None, weight="normal", anchor="nw", wrap=0):
            return canvas.create_text(x, y, text=value, fill=fill or INK, font=(self.sans, size, weight), anchor=anchor, width=wrap)

        left, right, pad, gap = p(30), width - p(30), p(20), p(14)
        inner = right - left - 2 * pad
        top = y = p(24) - self.run_scroll
        # How it is going: state, share done, the bar, time left.
        glyph, color, label = {"running": ("●", ACCENT_TEXT, self.t("run_running")), "waiting": ("○", MUTED, self.t("run_waiting")),
                               "done": ("✓", INK, self.t("run_done")), "error": ("⚠", ERROR, run["error"]),
                               "stopped": ("○", MUTED, self.t("run_stopped", time=span(now - run["updated"])))}[state]
        step, total = data["step"], data["total"]
        share = min(1, step / total) if step is not None and total else None
        hero = f"{100 * share:.0f} %" if share is not None else ""
        text(left + pad, y + pad, glyph, 10, color, "bold")
        text(left + pad + p(20), y + pad, self.clip(label, inner - p(30) - self.font(20, "bold").measure(hero), 10, "bold"), 10, weight="bold")
        text(right - pad, y + pad - p(8), hero, 20, weight="bold", anchor="ne")
        y += pad + p(26)
        if step is not None:
            text(left + pad, y, f"{figure(step)} / {figure(total)}" if total else self.t("run_step", step=figure(step)), 9, MUTED)
            y += p(24)
        if share is not None:
            fill = ACCENT if state in ("running", "done") else MUTED
            marks.append(lambda y=y: (box(left + pad, y, right - pad, y + p(8), CARD, p(4)),
                                      box(left + pad, y, left + pad + max(p(8), inner * share) * bool(share), y + p(8), fill, p(4))))
            y += p(20)
        left_for = runs.remaining(data, now)
        if state == "done":
            pace = self.t("run_done_at", clock=self.clock(run["updated"]))
        elif state == "waiting":
            pace = self.t("run_hint")
        elif state == "running" and share is not None:
            pace = self.t("run_estimating") if left_for is None else self.t("run_left", time=span(left_for), clock=self.clock(now + left_for))
        else:
            pace = ""
        age = "" if state in ("done", "waiting", "stopped") else self.t("run_updated", time=span(now - run["updated"]))
        if pace or age:
            room = inner - self.font(9).measure(age) - p(16)
            y = canvas.bbox(text(left + pad, y, pace or " ", 9, MUTED, wrap=room))[3]
            text(right - pad, y, age, 9, MUTED, anchor="se")
            y += p(6)
        if data["info"]:  # the latest value of everything that is not a curve
            y = canvas.bbox(text(left + pad, y + p(8), "   ·   ".join(f"{key} {value}" for key, value in data["info"].items()),
                                 9, wrap=inner))[3]
        y += pad - p(6)
        card(left, top, right, y)
        y += gap
        # One chart per metric: each has its own scale, so none is squeezed by another.
        names = list(data["series"])
        columns = max(1, min(len(names), int((right - left + gap) // (p(330) + gap))))
        cell, tall = (right - left - gap * (columns - 1)) / columns, p(192)
        small = self.font(8)
        self.run_plots = []
        for index, name in enumerate(names):
            points = data["series"][name]
            x0, y0 = left + (index % columns) * (cell + gap), y + (index // columns) * (tall + gap)
            card(x0, y0, x0 + cell, y0 + tall)
            latest = figure(points[-1][1])
            text(x0 + p(16), y0 + p(15), self.clip(name, cell - p(48) - self.font(15, "bold").measure(latest), 9, "bold"), 9, MUTED, "bold")
            text(x0 + cell - p(16), y0 + p(10), latest, 15, weight="bold", anchor="ne")
            scale = ticks(min(value for _, value in points), max(value for _, value in points))
            first, last = min(at for at, _ in points), max(at for at, _ in points)
            plot = (x0 + p(24) + max(small.measure(figure(value)) for value in scale), y0 + p(60), x0 + cell - p(22), y0 + tall - p(36))

            def across(at, plot=plot, first=first, last=last):
                return plot[2] if last == first else plot[0] + (at - first) / (last - first) * (plot[2] - plot[0])

            def up(value, plot=plot, low=scale[0], high=scale[-1]):
                return plot[3] - (value - low) / (high - low) * (plot[3] - plot[1])

            for value in scale:
                text(plot[0] - p(8), up(value), figure(value), 8, MUTED, anchor="e")
            text(plot[0], plot[3] + p(8), figure(first), 8, MUTED)
            if last != first:
                text(plot[2], plot[3] + p(8), figure(last), 8, MUTED, anchor="ne")

            def curve(points=points, plot=plot, scale=scale, across=across, up=up):
                for value in scale:
                    pen.line((plot[0] * over, up(value) * over, plot[2] * over, up(value) * over), fill=LINE, width=over)
                path = [(across(at) * over, up(value) * over) for at, value in points]
                if len(path) > 1:
                    pen.line(path, fill=ACCENT, width=p(2) * over, joint="curve")
                (x, y), ring, dot = path[-1], p(6) * over, p(4) * over  # the end dot keeps a ring of the card's colour
                pen.ellipse((x - ring, y - ring, x + ring, y + ring), fill=FIELD)
                pen.ellipse((x - dot, y - dot, x + dot, y + dot), fill=ACCENT)

            marks.append(curve)
            self.run_plots.append({"card": (x0, y0, x0 + cell, y0 + tall), "plot": plot, "points": points, "across": across, "up": up})
        if names:
            y += -(-len(names) // columns) * (tall + gap)
        if run["notes"]:
            top = y
            text(left + pad, y + pad, self.t("notes"), 8, MUTED, "bold")
            y += pad + p(24)
            when = max(self.font(9).measure(self.clock(moment)) for moment, _ in run["notes"]) + p(18)
            for moment, note in reversed(run["notes"]):  # newest first
                text(left + pad, y, self.clock(moment), 9, MUTED)
                y = canvas.bbox(text(left + pad + when, y, note, 10, wrap=inner - when))[3] + p(10)
            y += pad - p(10)
            card(left, top, right, y)
            y += gap
        self.run_height = y + self.run_scroll + p(10)
        for mark in marks:
            mark()
        self.photo = ImageTk.PhotoImage(sheet.resize((width, height), Image.Resampling.BOX))
        canvas.tag_lower(canvas.create_image(0, 0, image=self.photo, anchor="nw"))
        self.point_at()

    def point_at(self, event=False):
        """Under the pointer, a curve shows its nearest point and that point's value."""
        if event is not False:
            self.pointer = (event.x, event.y) if event else None
        self.canvas.delete("hover")
        if not (self.pointer and is_run(self.current)):
            return
        x, y = self.pointer
        for chart in self.run_plots:
            left, top, right, bottom = chart["card"]
            if left <= x <= right and top <= y <= bottom:
                at, value = min(chart["points"], key=lambda point: abs(chart["across"](point[0]) - x))
                px, py, radius = chart["across"](at), chart["up"](value), self.px(4)
                label = f"{figure(value)}  @  {figure(at)}"
                half = self.font(8).measure(label) / 2 + self.px(16)
                self.canvas.create_line(px, chart["plot"][1], px, chart["plot"][3], fill=EDGE, tags="hover")
                self.canvas.create_oval(px - radius, py - radius, px + radius, py + radius, fill=ACCENT, outline=FIELD,
                                        width=self.px(2), tags="hover")
                self.canvas.create_text(min(max(px, left + half), right - half), chart["plot"][1] - self.px(8), text=label,
                                        fill=INK, font=(self.sans, 8), anchor="s", tags="hover")

    def set_fit(self):
        self.fit = True
        self.draw()

    def actual_size(self):
        self.fit = False
        self.scale = 1
        self.pan = [0, 0]
        self.draw()

    def wheel(self, factor, event):
        """The wheel zooms a figure and scrolls the page of a live run."""
        if is_run(self.current):
            limit = max(0, self.run_height - self.canvas.winfo_height())
            self.run_scroll = max(0, min(limit, self.run_scroll + (-1 if factor > 1 else 1) * self.px(70)))
            self.draw()
        else:
            self.zoom(factor, event)

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
        if not hasattr(self, "drag_origin") or self.loading:
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
            self.select(self.pages[target])
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
                                          payload.get("deck", False), payload.get("agent_session"), payload.get("agent_cwd"),
                                          payload.get("agent_provider"), payload.get("agent_socket"))
                self.events.put(({"action": "received", "items": items, "focus": payload.get("focus", False), "watched_id": watched_id}, None, None))
            except Exception as exc:
                self.events.put(({"action": "notice", "message": str(exc)}, None, None))
            finally:
                self.events.put(({"action": "watch_done"}, None, None))
        threading.Thread(target=worker, daemon=True).start()

    def watch(self):
        try:
            if self.follow.get() and self.current and not self.watch_busy and not is_run(self.current):
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
                                **{key: item.get(key) for key in ("agent_session", "agent_cwd", "agent_provider", "agent_socket")}}, entry(item))
                            self.watch_candidate = None
                        else:
                            self.watch_candidate = candidate
                    else:
                        self.watch_candidate = None
        finally:
            self.root.after(900, self.watch)

    def spot(self, event):
        """The image pixel under the pointer, kept inside the image."""
        x = (event.x - self.image_origin[0]) / self.scale
        y = (event.y - self.image_origin[1]) / self.scale
        return (min(max(int(x), 0), self.original.width - 1), min(max(int(y), 0), self.original.height - 1)), (x, y)

    def mark(self, event):
        """Right button: point at an element of the figure to ask about it; dragging frames a region instead."""
        self.mark_from = None
        if not self.original:
            return
        point, (x, y) = self.spot(event)
        if not (0 <= x < self.original.width and 0 <= y < self.original.height):
            return
        if not self.current.get("agent_session"):
            if not self.chat_open:
                self.toggle_chat()  # the panel says why this figure cannot be asked about
            return
        self.mark_from = (event.x, event.y)
        self.point = point
        self.show_point()
        self.draw_marker()

    def mark_drag(self, event):
        if self.mark_from and self.original:
            # A hand that trembles while clicking still marks a point.
            if len(self.point) == 4 or max(abs(event.x - self.mark_from[0]), abs(event.y - self.mark_from[1])) >= self.px(6):
                self.point = (*self.point[:2], *self.spot(event)[0])
                self.show_point()
                self.draw_marker()

    def mark_end(self, event):
        if not self.mark_from:
            return
        self.mark_from = None
        if self.point and len(self.point) == 4:
            (x0, x1), (y0, y1) = sorted(self.point[::2]), sorted(self.point[1::2])
            self.point = (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else (x0, y0)
            self.show_point()
        # The panel opens once the mark is made: opening it earlier would move the figure under the pointer.
        if not self.chat_open:
            self.toggle_chat()
        self.draw_marker()
        self.chat_input.focus_set()

    def show_point(self):
        if self.point:
            if len(self.point) == 4:
                (x0, x1), (y0, y1) = sorted(self.point[::2]), sorted(self.point[1::2])
                self.point_label.configure(text=self.t("region", width=x1 - x0, height=y1 - y0, x=x0, y=y0))
            else:
                self.point_label.configure(text=self.t("point", x=self.point[0], y=self.point[1]))
            self.point_bar.pack(fill="x", pady=(0, 6), before=self.composer_ring)
        else:
            self.point_bar.pack_forget()

    def clear_point(self):
        self.point = None
        self.show_point()
        self.draw()

    def toggle_sidebar(self):
        self.sidebar_open = not self.sidebar_open
        self.store.set_setting("sidebar_open", self.sidebar_open)
        if self.sidebar_open:
            self.sidebar.pack(side="left", fill="y", before=self.main)
        else:
            self.sidebar.pack_forget()
        self.schedule_draw()

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
        self.save_chat_draft()
        self.codex_ready = False
        self.chat_probe = getattr(self, "chat_probe", 0) + 1
        is_codex = bool(item and item.get("agent_provider") == "codex")
        self.chat_mode = self.store.setting(f"chat_mode:{entry(item)}", "live") if is_codex else "figure"
        live = is_codex and self.chat_mode == "live"
        for mode, button in self.mode_buttons.items():
            self.skin(button, "chosen" if mode == self.chat_mode else "plain")
        self.thread_button.configure(text=self.t("reconnect" if live else "new_thread"))
        self.working_label.configure(text=self.t("working_live" if live else "working"))
        self.chat_entry = entry(item) if item else None
        self.chat_key = (self.chat_entry, self.chat_mode) if item else None
        self.thread = self.store.thread(self.chat_entry, self.chat_mode) if item else None
        self.chat_input.configure(state="normal")
        self.chat_input.delete("1.0", "end")
        self.chat_input.insert("1.0", self.chat_drafts.get(self.chat_key, ""))
        self.chat_log.configure(state="normal")
        self.chat_log.delete("1.0", "end")
        self.chat_log.configure(state="disabled")
        self.show_chat_controls(bool(item and item.get("agent_session")))
        if not item:
            self.chat_status.configure(text=self.t("chat_pick"))
        elif not item.get("agent_session"):
            self.chat_status.configure(text=self.t("chat_no_session_run" if is_run(item) else "chat_no_session"))
        else:
            status = "chat_checking" if live else "chat_figure" if is_codex else "chat_fork"
            self.chat_status.configure(text=self.t(status, id=item["agent_session"][:8]))
            if is_codex:
                threading.Thread(target=self.check_codex, args=(dict(item), self.chat_probe, not live,
                                                              self.thread["fork"] if self.thread else None), daemon=True).start()
            messages = self.store.messages(self.thread["id"]) if self.thread else []
            for message in messages:
                self.append_chat(message["role"], message["text"])
            if not messages:
                hint = "chat_hint_live" if live else "chat_hint_figure" if is_codex else "chat_hint_run" if is_run(item) else "chat_hint"
                self.append_chat("note", self.t(hint))
        self.update_chat_buttons()

    def save_chat_draft(self):
        if self.chat_key:
            self.chat_drafts[self.chat_key] = self.chat_input.get("1.0", "end-1c")

    def set_chat_mode(self, mode):
        if (mode in ("figure", "live") and self.current and self.current.get("agent_provider") == "codex"
                and mode != self.chat_mode):
            self.store.set_setting(f"chat_mode:{entry(self.current)}", mode)
            self.load_chat()

    def check_codex(self, item, probe, figure=False, fork=None):
        error = ""
        try:
            codex.available(item, figure=figure, fork=fork)
        except (OSError, ValueError, RuntimeError) as exc:
            error = str(exc)
        self.events.put(({"action": "codex_ready", "entry": entry(item), "probe": probe, "error": error}, None, None))

    def append_chat(self, role, text):
        self.chat_log.configure(state="normal")
        if role == "user":
            bubble = tk.Frame(self.chat_log, bg=CARD)
            self._label(bubble, text, justify="left", wraplength=int(self.chat.cget("width")) - self.px(96)).pack(padx=12, pady=8)
            self.round(bubble, 12)
            self.chat_log.window_create("end", window=bubble)
            self.chat_log.insert("end", "\n")
            self.chat_log.tag_add("user", "end-3c", "end-1c")
        else:
            self.chat_log.insert("end", {"tool": "·  ", "error": "⚠  "}.get(role, ""), role)
            # Answers come as light Markdown: render **bold** and `code`, leave the rest as written.
            for part in INLINE.split(text) if role == "assistant" else [text]:
                if part.startswith("**") and part.endswith("**") and len(part) > 4:
                    self.chat_log.insert("end", part[2:-2], (role, "bold"))
                elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                    self.chat_log.insert("end", part[1:-1], (role, "code"))
                else:
                    self.chat_log.insert("end", part, role)
            self.chat_log.insert("end", "\n", role)
        self.chat_log.insert("end", "\n", "gap")
        self.chat_log.configure(state="disabled")
        self.chat_log.see("end")

    def update_chat_buttons(self):
        usable = bool(self.current and self.current.get("agent_session"))
        is_codex = bool(self.current and self.current.get("agent_provider") == "codex")
        live = is_codex and self.chat_mode == "live"
        usable = usable and (not is_codex or self.codex_ready)
        running = bool(self.thread and self.thread["id"] in self.runs)
        self.send_button.configure(text=self.t("detach" if live else "stop") if running else self.t("send"),
                                   state="normal" if usable or running else "disabled")
        self.skin(self.send_button)
        if running and not self.working_label.winfo_manager():
            self.working_label.pack(fill="x", pady=(0, 6), before=self.point_bar if self.point_bar.winfo_manager() else self.composer_ring)
        elif not running:
            self.working_label.pack_forget()
        self.chat_input.configure(state="normal" if usable else "disabled")
        self.copy_button.configure(state="normal" if live or self.thread and self.thread["fork"] else "disabled")

    def chat_return(self, event):
        if event.state & 0x1:  # Shift+Enter keeps a new line
            return None
        self.send_or_stop()
        return "break"

    def new_thread(self):
        if self.current and self.current.get("agent_session"):
            if self.current.get("agent_provider") == "codex" and self.chat_mode == "live":
                self.load_chat()
                return
            self.store.new_thread(entry(self.current), self.chat_mode)
            self.load_chat()

    def copy_command(self):
        if self.current and self.current.get("agent_provider") == "codex":
            target = self.current["agent_session"] if self.chat_mode == "live" else self.thread["fork"] if self.thread else None
            if not target:
                return
            path = self.current.get("agent_socket") or codex.control_socket()
            line = f"codex --remote {shlex.quote('unix://' + path)} resume {shlex.quote(target)}"
        elif self.thread and self.thread["fork"] and self.current:
            cwd = chat.session_cwd(self.current["agent_session"], self.current.get("agent_cwd"))
            line = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(self.thread['fork'])}"
        else:
            return
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
        is_codex = item.get("agent_provider") == "codex"
        live = is_codex and self.chat_mode == "live"
        if is_codex and not self.codex_ready:
            return
        question = self.chat_input.get("1.0", "end").strip()
        if not question:
            return
        thread = self.thread or self.store.new_thread(entry(item), self.chat_mode)
        self.thread = thread
        if not self.store.messages(thread["id"]):
            self.chat_log.configure(state="normal")
            self.chat_log.delete("1.0", "end")
            self.chat_log.configure(state="disabled")
        self.chat_input.delete("1.0", "end")
        point, self.point = self.point, None
        region = bool(point) and len(point) == 4
        shown = (f"▭ ({point[0]}, {point[1]})–({point[2]}, {point[3]})  {question}" if region else
                 f"◉ ({point[0]}, {point[1]})  {question}" if point else question)
        self.store.add_message(thread["id"], "user", shown)
        self.append_chat("user", shown)
        if point:
            self.show_point()
            self.draw()
        try:
            if region:
                marked, zoom = chat.mark_region(item["snapshot"], point, self.store.directory / "points")
                point = dict(box=point, marked=marked, zoom=zoom)
            elif point:
                marked, zoom = chat.mark_point(item["snapshot"], *point, self.store.directory / "points")
                point = dict(x=point[0], y=point[1], marked=marked, zoom=zoom)
            readable = [self.store.assets, self.store.directory / "points"]
            if is_run(item):  # the fork reads what has been gathered from a file it may not be able to reach
                dump = self.store.directory / "runs" / f"{item['id']}.json"
                dump.parent.mkdir(exist_ok=True, mode=0o700)
                dump.write_text(json.dumps(self.store.run(item["id"]), ensure_ascii=False))
                item = dict(item, dump=str(dump))
                readable.append(dump.parent)
            prompt = chat.prompt(question, item, not thread["fork"], point, live=live)
            if is_codex:
                observer = codex.Question(figure=not live, fork=thread["fork"])
                self.runs[thread["id"]] = observer
                self.update_chat_buttons()
                threading.Thread(target=self.codex_worker, args=(thread["id"], observer, dict(item), prompt), daemon=True).start()
                return
            args = chat.command(item["agent_session"], thread["fork"], prompt, readable)
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

    def codex_worker(self, thread, observer, item, prompt):
        def emit(kind, text):
            if kind == "session":
                self.store.set_fork(thread, text)
            else:
                self.store.add_message(thread, "assistant" if kind == "text" else kind, text)
            self.events.put(({"action": "chat", "thread": thread, "kind": kind, "text": text}, None, None))
        try:
            for kind, text in observer.events(item, prompt):
                emit(kind, self.t("live_" + text) if kind == "note" else text)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            emit("error", self.t("figure_error" if observer.figure else "live_error", error=str(exc)))
        finally:
            self.events.put(({"action": "chat", "thread": thread, "kind": "end", "text": ""}, None, None))

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
                stopped = bool(process.returncode and process.returncode < 0)
                message = self.t("stopped") if stopped else (detail or self.t("exit_code", code=process.returncode))
                kind = "note" if stopped else "error"  # stopping was the user's choice, not a failure
                self.store.add_message(thread, kind, message)
                self.events.put(({"action": "chat", "thread": thread, "kind": kind, "text": message}, None, None))
        finally:
            process.stdout.close()
            errors.close()
            self.events.put(({"action": "chat", "thread": thread, "kind": "end", "text": ""}, None, None))

    def dispatch(self, payload):
        action = payload["action"]
        if action == "codex_ready":
            if (self.current and entry(self.current) == payload["entry"] and self.current.get("agent_provider") == "codex"
                    and self.chat_probe == payload["probe"]):
                self.codex_ready = not payload["error"]
                status = "chat_live" if self.chat_mode == "live" else "chat_figure"
                offline = "chat_offline" if self.chat_mode == "live" else "chat_figure_offline"
                self.chat_status.configure(text=self.t(offline, error=payload["error"]) if payload["error"] else
                                           self.t(status, id=self.current["agent_session"][:8]))
                self.update_chat_buttons()
            return
        if action == "status":
            return {"ok": True, "running": True, "pid": os.getpid(), "session": self.session,
                    "current_id": self.current["id"] if self.current else None, "following": self.follow.get(),
                    "mirrors": {host: link["state"] for host, link in self.mirrors.items()}}
        if action == "stop":
            self.root.after(120, self.close)
            return {"ok": True, "running": False}
        if action == "follow":
            self.follow_host(payload["host"], not payload.get("off"))
            return self.dispatch({"action": "status"})
        if action in ("show", "received", "track"):
            items = payload["items"]
            if action == "track":
                self.follow_run(items[0]["id"])
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
                self.select(target)
                self.history_canvas.yview_moveto(0)
                self.new_button.pack_forget()
            else:
                self.refresh_history()
                self.new_button.pack(before=self.canvas, padx=26, pady=(0, 12), anchor="w")
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
                elif payload["kind"] in ("text", "tool", "error", "note"):
                    self.append_chat("assistant" if payload["kind"] == "text" else payload["kind"], payload["text"])
                self.update_chat_buttons()
        if action == "run":
            self.following.add(payload["id"])  # a relaunch into the same file carries on under a new id
            run = self.store.run(payload["id"])
            known = next((index for index, item in enumerate(self.items) if item["id"] == run["id"]), None)
            shown = bool(self.current and self.current["id"] == run["id"])
            if known is not None and self.items[known]["closed"] == run["closed"]:
                self.items[known] = run
                self.paint_run_row(run)
            elif run["session"] == self.session:
                # A relaunch of the run being watched takes its place on screen.
                shown = shown or bool(known is None and self.follow.get() and is_run(self.current)
                                      and self.current["source"] == run["source"])
                self.refresh_sessions()
                self.refresh_history()
            if shown and self.current["id"] != run["id"]:
                self.select(run)
            elif shown:
                self.current = run
                self.schedule_draw()
        if action == "run_end":
            self.following -= {payload["id"], payload["started"]}
            run = self.store.run(payload["id"])
            if run and not run["closed"] and not payload["failed"]:  # followed again just as its thread was leaving
                self.follow_run(run["id"])
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
        for process in [*self.runs.values(), *self.feeds.values()]:
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
            if not isinstance(payload, dict) or payload.get("action") not in {"status", "show", "open", "stop", "track", "follow"}:
                raise ValueError("Unknown action.")
            if payload["action"] == "follow" and not (isinstance(payload.get("host"), str) and mirror.HOST.fullmatch(payload["host"])):
                raise ValueError("host must be a host name.")
            if payload["action"] in {"show", "track"}:
                for key in ("agent_session", "agent_cwd", "agent_provider", "agent_socket"):
                    if payload.get(key) is not None and not isinstance(payload[key], str):
                        raise ValueError(f"{key} must be a string.")
                if payload.get("agent_provider") not in (None, "claude", "codex"):
                    raise ValueError("Unknown agent provider.")
            if payload["action"] == "show":
                paths = payload.get("paths")
                if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                    raise ValueError("paths must be a list of paths.")
                for key in ("session", "title", "caption"):
                    if key in payload and not isinstance(payload[key], str):
                        raise ValueError(f"{key} must be a string.")
                if not isinstance(payload.get("deck", False), bool):
                    raise ValueError("deck must be true or false.")
                payload["items"] = self.server.app.store.ingest(paths, payload.get("session", "Images"), payload.get("title", ""),
                    payload.get("caption", ""), payload.get("deck", False), payload.get("agent_session"), payload.get("agent_cwd"),
                    payload.get("agent_provider"), payload.get("agent_socket"))
            if payload["action"] == "track":
                for key in ("source", "session", "title", "note"):
                    if not isinstance(payload.get(key, ""), str):
                        raise ValueError(f"{key} must be a string.")
                metrics = payload.get("metrics")
                if metrics is not None and not (isinstance(metrics, list) and all(isinstance(key, str) for key in metrics)):
                    raise ValueError("metrics must be a list of names.")
                store = self.server.app.store
                run = store.track(payload.get("source", ""), payload.get("session", "Images"), payload.get("title", ""), metrics,
                                  payload.get("agent_session"), payload.get("agent_cwd"), payload.get("agent_provider"), payload.get("agent_socket"))
                run, _ = runs.advance(store, run["id"])  # read it now, so the answer says what was understood
                if payload.get("note"):
                    store.add_note(run["id"], payload["note"])
                    run = store.run(run["id"])
                payload["items"] = [run]
            done, result = threading.Event(), {}
            self.server.app.events.put((payload, done, result))
            if not done.wait(16):
                raise RuntimeError("The window is busy. Check visor status.")
            if payload["action"] == "track":
                result.update(runs.summary(run))
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
