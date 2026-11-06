"""Native Tk window. All widget operations stay on the Tk event thread."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import socketserver
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
import traceback

from PIL import Image, ImageDraw, ImageTk

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


class Visor:
    def __init__(self, root, store):
        self.root = root
        self.store = store
        self.events = queue.Queue()
        self.session = None
        self.items = []
        self.current = None
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
        root.title("Visor · Tu espacio visual")
        root.geometry(store.setting("geometry", "1120x760"))
        root.minsize(820, 540)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._icon()
        self.refresh_sessions()
        saved_session = store.setting("session")
        if saved_session in [entry['session'] for entry in self.sessions]:
            self.select_session(saved_session)
        elif self.sessions:
            self.select_session(self.sessions[0]["session"])
        else:
            self.draw()
        root.bind("<Left>", lambda event: self.step(1))
        root.bind("<Right>", lambda event: self.step(-1))
        root.bind("<Key-f>", lambda event: self.set_fit())
        root.bind("<Key-1>", lambda event: self.actual_size())
        root.bind("<Control-o>", lambda event: self.open_files())
        root.bind("<Control-plus>", lambda event: self.zoom(1.2))
        root.bind("<Control-equal>", lambda event: self.zoom(1.2))
        root.bind("<Control-minus>", lambda event: self.zoom(1 / 1.2))
        root.after(50, self.poll_events)
        root.after(900, self.watch)

    def _label(self, parent, text, fg=INK, bg=None, size=10, weight="normal", **kwargs):
        return tk.Label(parent, text=text, fg=fg, bg=bg or parent.cget("bg"),
                        font=("DejaVu Sans", size, weight), **kwargs)

    def _button(self, parent, text, command, primary=False, light=False):
        bg = ACCENT if primary else ("#e1e7df" if light else CARD)
        fg = DARK if primary or light else INK
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground="#a6e6d0" if primary else ("#d4ded3" if light else SELECTED),
                         activeforeground=fg, relief="flat", bd=0, highlightthickness=0,
                         cursor="hand2", font=("DejaVu Sans", 10), padx=13, pady=7)

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
        self._label(brand, "Un espacio para tus resultados", fg=MUTED, size=9).pack(anchor="w")
        self._button(header, "Abrir imagen  +", self.open_files).pack(side="right", padx=22)
        self._label(header, "●  Listo para recibir", fg=ACCENT, size=9).pack(side="right", padx=10)
        tk.Frame(self.root, height=1, bg="#2a3b40").pack(fill="x")
        body = tk.Frame(self.root, bg=PANEL)
        body.pack(fill="both", expand=True)
        sidebar = tk.Frame(body, bg=PANEL, width=264)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._label(sidebar, "PROYECTO", fg=MUTED, size=8, weight="bold").pack(anchor="w", padx=20, pady=(22, 10))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Visor.TCombobox", fieldbackground=CARD, background=CARD,
                        foreground=INK, arrowcolor=ACCENT, borderwidth=0, padding=7)
        style.map("Visor.TCombobox", fieldbackground=[("readonly", CARD)],
                  selectbackground=[("readonly", CARD)], selectforeground=[("readonly", INK)])
        self.session_var = tk.StringVar()
        self.session_box = ttk.Combobox(sidebar, textvariable=self.session_var, state="readonly",
                                       style="Visor.TCombobox", font=("DejaVu Sans", 10))
        self.session_box.pack(fill="x", padx=17)
        self.session_box.bind("<<ComboboxSelected>>", self.on_session_changed)
        tk.Checkbutton(sidebar, text="Seguir lo nuevo", variable=self.follow, command=self.follow_changed,
            bg=PANEL, fg=INK, selectcolor=CARD, activebackground=PANEL, activeforeground=INK,
            font=("DejaVu Sans", 9), highlightthickness=0, cursor="hand2").pack(anchor="w", padx=17, pady=15)
        history_title = tk.Frame(sidebar, bg=PANEL)
        history_title.pack(fill="x", padx=20, pady=(5, 10))
        self._label(history_title, "HISTORIAL", fg=MUTED, size=8, weight="bold").pack(side="left")
        self.count_label = self._label(history_title, "0", fg=MUTED, size=8)
        self.count_label.pack(side="right")
        hist_area = tk.Frame(sidebar, bg=PANEL)
        hist_area.pack(fill="both", expand=True, padx=(10, 0))
        self.history_canvas = tk.Canvas(hist_area, bg=PANEL, highlightthickness=0, bd=0, width=244)
        self.history_canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(hist_area, orient="vertical", command=self.history_canvas.yview)
        scroll.pack(side="right", fill="y")
        self.history_canvas.configure(yscrollcommand=scroll.set)
        self.history_frame = tk.Frame(self.history_canvas, bg=PANEL)
        self.history_window = self.history_canvas.create_window(0, 0, window=self.history_frame, anchor="nw")
        self.history_frame.bind("<Configure>", lambda event: self.history_canvas.configure(scrollregion=self.history_canvas.bbox("all")))
        self.history_canvas.bind("<Configure>", lambda event: self.history_canvas.itemconfigure(self.history_window, width=event.width))
        self._label(sidebar, "LOCAL  /  EN TU EQUIPO", fg=MUTED, size=8).pack(anchor="w", padx=20, pady=18)
        main = tk.Frame(body, bg=PAPER)
        main.pack(side="left", fill="both", expand=True)
        heading = tk.Frame(main, bg=PAPER)
        heading.pack(fill="x", padx=28, pady=(25, 15))
        self.title_label = self._label(heading, "Tu próxima idea, aquí.", fg=DARK, size=19, weight="bold", anchor="w")
        self.title_label.pack(fill="x")
        self.caption_label = self._label(heading, "Imágenes y gráficos, directamente desde tu terminal.", fg="#63766c", size=10, anchor="w", justify="left", wraplength=650)
        self.caption_label.pack(fill="x", pady=(7, 0))
        self.new_button = self._button(main, "Hay nuevas imágenes · Ver última", self.show_latest, primary=True)
        self.canvas = tk.Canvas(main, bg="#e6ebe3", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True, padx=23)
        self.canvas.bind("<Configure>", self.schedule_draw)
        self.canvas.bind("<Button-4>", lambda event: self.zoom(1.15, event))
        self.canvas.bind("<Button-5>", lambda event: self.zoom(1 / 1.15, event))
        self.canvas.bind("<MouseWheel>", lambda event: self.zoom(1.15 if event.delta > 0 else 1 / 1.15, event))
        self.canvas.bind("<ButtonPress-1>", self.pan_start)
        self.canvas.bind("<B1-Motion>", self.pan_move)
        self.canvas.bind("<Double-Button-1>", lambda event: self.set_fit())
        controls = tk.Frame(main, bg=PAPER)
        controls.pack(fill="x", padx=23, pady=(12, 7))
        self._button(controls, "←", lambda: self.step(1), light=True).pack(side="left")
        self._button(controls, "→", lambda: self.step(-1), light=True).pack(side="left", padx=(5, 13))
        self.position_label = self._label(controls, "", fg="#63766c", size=9)
        self.position_label.pack(side="left")
        self._button(controls, "Ajustar", self.set_fit, light=True).pack(side="right")
        self._button(controls, "1:1", self.actual_size, light=True).pack(side="right", padx=5)
        self._button(controls, "+", lambda: self.zoom(1.2), light=True).pack(side="right")
        self.zoom_label = self._label(controls, "100 %", fg=DARK, size=9, width=7)
        self.zoom_label.pack(side="right")
        self._button(controls, "−", lambda: self.zoom(1 / 1.2), light=True).pack(side="right")
        self.detail_label = self._label(main, "La ventana se abre sola cuando tu agente muestra algo.", fg="#748277", size=8, anchor="w")
        self.detail_label.pack(fill="x", padx=25, pady=(2, 16))

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
        self.new_button.pack_forget()

    def refresh_history(self):
        self.items = self.store.history(self.session, 200) if self.session else []
        self.count_label.configure(text=str(len(self.items)))
        for child in self.history_frame.winfo_children():
            child.destroy()
        self.thumbnails.clear()
        self.cards = {}
        for index, item in enumerate(self.items):
            active = self.current and item["id"] == self.current["id"]
            bg = SELECTED if active else CARD
            card = tk.Frame(self.history_frame, bg=bg, padx=9, pady=9, cursor="hand2")
            card.pack(fill="x", padx=(4, 9), pady=(0, 7))
            self.cards[item["id"]] = card
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
            self._label(card, "ÚLTIMA" if index == 0 else f"VERSIÓN {len(self.items) - index}", fg=ACCENT if index == 0 else MUTED,
                        bg=bg, size=7, anchor="w").pack(anchor="w", pady=(6, 0))
            def bind_tree(widget, chosen=item):
                widget.bind("<Button-1>", lambda event, value=chosen: self.select(value))
                widget.bind("<Button-4>", lambda event: self.history_canvas.yview_scroll(-3, "units"))
                widget.bind("<Button-5>", lambda event: self.history_canvas.yview_scroll(3, "units"))
                for child in widget.winfo_children():
                    bind_tree(child, chosen)
            bind_tree(card)

    def select(self, item, rebuild=True):
        if self.original:
            self.original.close()
        self.current = item
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
        self.detail_label.configure(text=f"{item['width']} × {item['height']} px   ·   {item['source']}")
        if rebuild:
            self.refresh_history()
        else:
            for key, card in self.cards.items():
                color = SELECTED if key == item["id"] else CARD
                self._recolor(card, color)
        index = next((i for i, value in enumerate(self.items) if value["id"] == item["id"]), 0)
        self.position_label.configure(text=f"{len(self.items) - index} / {len(self.items)}")
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

    def draw(self):
        self.pending_draw = None
        canvas = self.canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if not self.original:
            canvas.create_oval(width / 2 - 28, height / 2 - 70, width / 2 + 28, height / 2 - 14, fill="#d3e3d6", outline="")
            canvas.create_text(width / 2, height / 2 - 42, text="V", fill="#507d65", font=("DejaVu Sans", 24, "bold"))
            canvas.create_text(width / 2, height / 2 + 10, text="Todo empieza con una imagen.", fill=DARK, font=("DejaVu Sans", 15, "bold"))
            canvas.create_text(width / 2, height / 2 + 46, text="Pídele a tu agente que te enseñe lo que está haciendo.", fill="#687e6d", font=("DejaVu Sans", 10))
            return
        if self.fit:
            self.scale = min((max(width, 80) - 36) / self.original.width,
                             (max(height, 80) - 36) / self.original.height, 1.0)
            self.pan = [0, 0]
        # Render only the visible crop: zooming a large image never allocates a giant bitmap.
        scaled_w, scaled_h = self.original.width * self.scale, self.original.height * self.scale
        left = (width - scaled_w) / 2 + self.pan[0]
        top = (height - scaled_h) / 2 + self.pan[1]
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
        self.drag_origin = (event.x, event.y, self.pan.copy())

    def pan_move(self, event):
        if not hasattr(self, "drag_origin"):
            return
        x, y, pan = self.drag_origin
        self.fit = False
        self.pan = [pan[0] + event.x - x, pan[1] + event.y - y]
        self.draw()

    def step(self, direction):
        if not self.items or not self.current:
            return
        index = next((i for i, item in enumerate(self.items) if item["id"] == self.current["id"]), 0)
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
        paths = filedialog.askopenfilenames(title="Mostrar imágenes", filetypes=[("Imágenes", "*.png *.jpg *.jpeg *.webp *.gif *.tif *.tiff *.bmp *.ico"), ("Todos los archivos", "*")])
        if paths:
            self.ingest_in_background({"paths": list(paths), "session": self.session or "Imágenes"})

    def ingest_in_background(self, payload, watched_id=None):
        def worker():
            try:
                items = self.store.ingest(payload["paths"], payload["session"], payload.get("title", ""), payload.get("caption", ""))
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
                if self.store.latest_for_source(item["session"], item["source"]) == item["id"]:
                    try:
                        value = signature(Path(item["source"]))
                    except OSError:
                        value = None
                    if value and value != item["signature"]:
                        candidate = (item["id"], value)
                        if candidate == self.watch_candidate:
                            self.watch_busy = True
                            self.ingest_in_background({"paths": [item["source"]], "session": item["session"], "title": item["title"], "caption": item["caption"]}, item["id"])
                            self.watch_candidate = None
                        else:
                            self.watch_candidate = candidate
                    else:
                        self.watch_candidate = None
        finally:
            self.root.after(900, self.watch)

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
            self.refresh_sessions()
            if not self.current or (self.follow.get() and (not watched_id or self.current["id"] == watched_id)):
                self.session = items[-1]["session"]
                self.store.set_setting("session", self.session)
                self.current = items[-1]
                self.refresh_sessions()
                self.refresh_history()
                self.select(items[-1], rebuild=False)
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
                    "queued": self.current["id"] != items[-1]["id"]}
        if action == "open":
            if payload.get("session"):
                if payload["session"] not in [row["session"] for row in self.sessions]:
                    raise ValueError("Ese proyecto aún no tiene imágenes.")
                self.select_session(payload["session"])
            self.root.deiconify()
            self.root.lift()
            return self.dispatch({"action": "status"})
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
        self.store.set_setting("geometry", self.root.geometry())
        self.store.set_setting("follow", self.follow.get())
        self.root.destroy()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(20)
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                raise ValueError("Petición incompleta o demasiado grande.")
            payload = json.loads(line)
            if not isinstance(payload, dict) or payload.get("action") not in {"status", "show", "open", "stop"}:
                raise ValueError("Acción desconocida.")
            if payload["action"] == "show":
                paths = payload.get("paths")
                if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                    raise ValueError("paths debe ser una lista de rutas.")
                for key in ("session", "title", "caption"):
                    if key in payload and not isinstance(payload[key], str):
                        raise ValueError(f"{key} debe ser texto.")
                payload["items"] = self.server.app.store.ingest(paths, payload.get("session", "Imágenes"), payload.get("title", ""), payload.get("caption", ""))
            done, result = threading.Event(), {}
            self.server.app.events.put((payload, done, result))
            if not done.wait(16):
                raise RuntimeError("La ventana está ocupada. Comprueba visor status.")
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
