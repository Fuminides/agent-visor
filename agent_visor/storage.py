"""Persistent, immutable image snapshots. No GUI imports in this module."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "TIFF", "BMP", "ICO")
MAX_PIXELS = 24_000_000
MAX_FILE_BYTES = 100 * 1024 * 1024


def data_dir() -> Path:
    override = os.environ.get("VISOR_DATA_DIR")
    return Path(override).expanduser() if override else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "agent-visor"


def signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"


def load_image(path: Path) -> Image.Image:
    if not path.is_file():
        raise ValueError(f"No existe el archivo: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"La imagen supera los 100 MB: {path.name}")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path, formats=FORMATS) as image:
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError(f"La imagen supera los 24 megapíxeles: {path.name}")
                image.seek(0)
                image.load()
                return ImageOps.exif_transpose(image).convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError(f"No se puede leer {path.name}. Usa PNG, JPEG, WebP, GIF, TIFF o BMP.") from exc


class Store:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or data_dir()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.assets = self.directory / "images"
        self.assets.mkdir(exist_ok=True, mode=0o700)
        self.db = self.directory / "history.sqlite3"
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, title TEXT NOT NULL,
                    caption TEXT NOT NULL, source TEXT NOT NULL, snapshot TEXT NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL,
                    created REAL NOT NULL, signature TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS images_session_created ON images(session, created DESC);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def ingest(self, paths, session, title="", caption=""):
        if not isinstance(session, str) or not session.strip() or len(session) > 200:
            raise ValueError("El proyecto debe tener entre 1 y 200 caracteres.")
        if not paths or len(paths) > 30:
            raise ValueError("Envía entre 1 y 30 imágenes por llamada.")
        prepared = []
        written = []
        try:
            for value in paths:
                path = Path(value).expanduser().resolve(strict=True)
                before = signature(path)
                image = load_image(path)
                after = signature(path)
                if before != after:
                    raise ValueError(f"El archivo está cambiando; vuelve a intentarlo: {path.name}")
                item_id = uuid.uuid4().hex
                snapshot = self.assets / f"{item_id}.png"
                image.save(snapshot, "PNG")
                written.append(snapshot)
                thumbnail = image.copy()
                thumbnail.thumbnail((124, 90))
                thumb_path = snapshot.with_suffix(".thumb.png")
                thumbnail.save(thumb_path, "PNG")
                thumbnail.close()
                written.append(thumb_path)
                prepared.append(dict(id=item_id, session=session.strip(), title=(title or path.stem)[:300],
                    caption=caption[:2000], source=str(path), snapshot=str(snapshot),
                    width=image.width, height=image.height, created=time.time(), signature=after))
                image.close()
            with self.connect() as conn:
                conn.executemany("""INSERT INTO images
                    (id,session,title,caption,source,snapshot,width,height,created,signature)
                    VALUES (:id,:session,:title,:caption,:source,:snapshot,:width,:height,:created,:signature)""", prepared)
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return prepared

    def sessions(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT session,COUNT(*) AS count,MAX(created) AS updated FROM images GROUP BY session ORDER BY updated DESC")]

    def history(self, session=None, limit=200):
        with self.connect() as conn:
            if session is None:
                rows = conn.execute("SELECT * FROM images ORDER BY created DESC LIMIT ?", (limit,))
            else:
                rows = conn.execute("SELECT * FROM images WHERE session=? ORDER BY created DESC LIMIT ?", (session, limit))
            return [dict(row) for row in rows]

    def get(self, item_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM images WHERE id=?", (item_id,)).fetchone()
            return dict(row) if row else None

    def latest_for_source(self, session, source):
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM images WHERE session=? AND source=? ORDER BY created DESC LIMIT 1", (session, source)).fetchone()
            return row["id"] if row else None

    def setting(self, key, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row:
            try:
                return json.loads(row["value"])
            except json.JSONDecodeError:
                pass
        return default

    def set_setting(self, key, value):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))
