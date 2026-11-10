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
from tkinter import filedialog, font as tkfont, ttk
import traceback

from PIL import Image, ImageDraw, ImageOps, ImageTk

from . import chat, i18n
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


def visor_mark(size, fill, stroke, background=None, square=False):
    """The Visor mark (a V in a circle, or in a rounded square for the app icon), drawn at 4x and reduced
    so its curves stay smooth; Tk's own canvas shapes are not antialiased."""
    scale = 4
    side = size * scale
    image = Image.new("RGBA", (side, side), background or (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if square:  # same proportions as assets/visor.svg
        draw.rounded_rectangle((0, 0, side - 1, side - 1), radius=side * 30 // 128, fill=fill)
        points, width = [(side * .3125, side * .367), (side * .5, side * .656), (side * .6875, side * .367)], side * .078
    else:
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


def ring(parent):
    """Focus-ring colour with at least 3:1 contrast against the background the control sits on."""
    red, green, blue = (value / 65535 for value in parent.winfo_rgb(parent.cget("bg")))
    return "#1f1e1b" if .2126 * red + .7152 * green + .0722 * blue > .5 else "#f3f1ea"


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
        self.point = None
        self.image_origin = (0, 0)
        self.runs = {}
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
        root.after(50, self.poll_events)
        root.after(900, self.watch)

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
            self.select(self.current, rebuild=False)
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
        self.canvas.bind("<Button-4>", lambda event: self.zoom(1.15, event))
        self.canvas.bind("<Button-5>", lambda event: self.zoom(1 / 1.15, event))
        self.canvas.bind("<MouseWheel>", lambda event: self.zoom(1.15 if event.delta > 0 else 1 / 1.15, event))
        self.canvas.bind("<ButtonPress-1>", self.pan_start)
        self.canvas.bind("<B1-Motion>", self.pan_move)
        self.canvas.bind("<Double-Button-1>", lambda event: self.set_fit())
        self.canvas.bind("<Button-3>", self.mark)
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
        return canvas, frame

    def _row(self, canvas, frame, image, title, meta, command, selected=False, indent=0, tags="", tags_fg=None):
        """One clickable row of a sidebar list: thumbnail, title and a quieter second line."""
        row = tk.Frame(frame, bg=PANEL, cursor="hand2")
        row.pack(fill="x", padx=(8 + indent, 4), pady=1)
        row.rounded = False
        if image:
            tk.Label(row, image=image, bg=PANEL, bd=0).pack(side="left", padx=(8, 10), pady=7, anchor="n")
        else:
            self._label(row, "◇", fg=ACCENT, size=16).pack(side="left", padx=(8, 10), pady=7, anchor="n")
        text = tk.Frame(row, bg=PANEL)
        text.pack(side="left", fill="x", expand=True, pady=6)
        room = self.px(SIDEBAR - 52 - indent) - (image.width() if image else self.px(20))
        self._label(text, title, size=9, weight="bold", anchor="w", wraplength=room, justify="left").pack(anchor="w")
        self._label(text, meta, fg=MUTED, size=8).pack(anchor="w", pady=(2, 0))
        if tags:
            self._label(text, tags, fg=tags_fg or MUTED, size=7).pack(anchor="w", pady=(3, 0))

        def bind_tree(widget):
            widget.bind("<Button-1>", lambda event: command())
            widget.bind("<Enter>", lambda event: self.hover_row(row))
            widget.bind("<Button-4>", lambda event: canvas.yview_scroll(-3, "units"))
            widget.bind("<Button-5>", lambda event: canvas.yview_scroll(3, "units"))
            for child in widget.winfo_children():
                bind_tree(child)
        bind_tree(row)
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
                widget = self._row(self.browser_canvas, self.browser_frame, self.thumbnail(row["snapshot"], self.px(44), self.px(32)),
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
        actions = self.chat_actions = tk.Frame(self.chat, bg=PANEL)
        self._button(actions, self.t("new_thread"), self.new_thread, size=9, padx=11, pady=5).pack(side="left")
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
        for widget in (self.chat_actions, self.chat_bottom, self.chat_log):
            widget.pack_forget()
        if visible:
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
        self.window_icons = [ImageTk.PhotoImage(visor_mark(size, *ICON, square=True)) for size in (256, 128, 64, 48, 32, 16)]
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
        if not self.current or not any(entry(item) == entry(self.current) for item in self.items):
            if self.items:
                self.select(self.items[0], rebuild=False)
            else:
                self.clear_view()

    def paint_periods(self):
        for key, button in self.period_buttons.items():
            self.skin(button, "chosen" if key == self.period else "plain")

    def refresh_history(self):
        self.items = self.store.history(self.session, 200, collapse=True, since=self.since()) if self.session else []
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
            self.slide_bar.pack(side="left")
        else:
            self.slide_bar.pack_forget()
        if rebuild:
            self.refresh_history()
        else:
            for key, card in self.cards.items():
                self.paint_row(card, key == entry(item))
        self.update_position()
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
        self.caption_label.configure(wraplength=max(self.px(300), self.canvas.winfo_width() - self.px(60)))
        self.title_label.configure(wraplength=max(self.px(240), self.canvas.winfo_width() - self.px(60)))

    def draw(self):
        self.pending_draw = None
        canvas = self.canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
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
            self.scale = min((max(width, 80) - 56) / self.original.width,
                             (max(height, 80) - 56) / self.original.height, 1.0)
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
            crop = self.original.crop(box)
            size = (max(1, round(crop.width * self.scale)), max(1, round(crop.height * self.scale)))
            rendered = crop.resize(size, Image.Resampling.LANCZOS)
            self.photo = ImageTk.PhotoImage(rendered)
            canvas.create_image(left + box[0] * self.scale, top + box[1] * self.scale, image=self.photo, anchor="nw")
        if self.point:
            x, y = left + self.point[0] * self.scale, top + self.point[1] * self.scale
            r = self.px(11)
            canvas.create_oval(x - r, y - r, x + r, y + r, outline="white", width=self.px(6), tags="marker")
            canvas.create_oval(x - r, y - r, x + r, y + r, outline="#e5484d", width=self.px(3), tags="marker")
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
        running = bool(self.thread and self.thread["id"] in self.runs)
        self.send_button.configure(text=self.t("stop") if running else self.t("send"), state="normal" if usable else "disabled")
        self.skin(self.send_button)
        if running and not self.working_label.winfo_manager():
            self.working_label.pack(fill="x", pady=(0, 6), before=self.point_bar if self.point_bar.winfo_manager() else self.composer_ring)
        elif not running:
            self.working_label.pack_forget()
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
