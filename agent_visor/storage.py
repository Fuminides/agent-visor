"""Persistent, immutable image snapshots. No GUI imports in this module."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "TIFF", "BMP", "ICO")
MAX_PIXELS = 24_000_000
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_PAGES = 100
PDF_LONG_SIDE = 2400


def data_dir() -> Path:
    override = os.environ.get("VISOR_DATA_DIR")
    return Path(override).expanduser() if override else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "agent-visor"


def signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"


def check_size(path: Path):
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"File is larger than 100 MB: {path.name}")


def load_image(path: Path) -> Image.Image:
    check_size(path)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path, formats=FORMATS) as image:
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError(f"Image is larger than 24 megapixels: {path.name}")
                image.seek(0)
                image.load()
                return ImageOps.exif_transpose(image).convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError(f"Cannot read {path.name}. Use PDF, PNG, JPEG, WebP, GIF, TIFF or BMP.") from exc


def is_pdf(path: Path) -> bool:
    check_size(path)
    with path.open("rb") as handle:
        return handle.read(5) == b"%PDF-"


def pdf_page_count(path: Path) -> int:
    if not (shutil.which("pdfinfo") and shutil.which("pdftoppm")):
        raise ValueError("Showing PDFs needs poppler-utils (pdfinfo and pdftoppm).")
    result = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=60)
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.M)
    if result.returncode or not match or int(match.group(1)) < 1:
        raise ValueError(f"Cannot read the PDF {path.name}.")
    return int(match.group(1))


def page_count(path: Path) -> int:
    """Validate a file without keeping it in memory; return how many slides it adds."""
    if is_pdf(path):
        return pdf_page_count(path)
    load_image(path).close()
    return 1


def load_pages(path: Path):
    """Yield one image per page: one for a picture, every page for a PDF."""
    if not is_pdf(path):
        yield load_image(path)
        return
    pdf_page_count(path)
    with tempfile.TemporaryDirectory(prefix="visor-pdf-") as tmp:
        result = subprocess.run(["pdftoppm", "-png", "-scale-to", str(PDF_LONG_SIDE), str(path), f"{tmp}/page"],
                                capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise ValueError(f"Cannot convert the PDF {path.name}: {result.stderr.strip()[:300]}")
        # pdftoppm pads page numbers to equal width, so name order is page order.
        for page in sorted(Path(tmp).glob("page-*.png")):
            yield load_image(page)


def conversation(session: str) -> tuple[str, str]:
    """(project, conversation) the window files a history group under.
    "Study / comparison" is the conversation "comparison" of the project "Study". A folder belongs to its git
    repository: a subfolder or a linked worktree is a conversation of the main repository's project."""
    if not session.startswith("/"):
        project, _, label = session.partition(" / ")
        return project.strip() or session, label.strip()
    path = Path(session)
    for root in (path, *path.parents):
        if root == Path.home():  # a repository of dotfiles in the home folder is not a project
            break
        marker = root / ".git"
        try:
            if marker.is_file():  # linked worktree: "gitdir: <main>/.git/worktrees/<name>"
                match = re.match(r"gitdir: (.+)/\.git/worktrees/", marker.read_text(errors="replace"))
                if match:
                    main = Path(match.group(1)).name
                    name = root.name.removeprefix(main).lstrip("-_ ") or root.name
                    return main, "/".join([name, *path.relative_to(root).parts])
            if marker.exists():
                return root.name, "/".join(path.relative_to(root).parts)
        except OSError:
            break
    return path.name or session, ""


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
            # Slides of one presentation share a deck id; single images keep these columns NULL.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(images)")}
            # agent_session/agent_cwd: the Claude Code session that showed the image, for the chat panel.
            for name, kind in (("deck", "TEXT"), ("page", "INTEGER"), ("pages", "INTEGER"),
                               ("agent_session", "TEXT"), ("agent_cwd", "TEXT")):
                if name not in columns:
                    try:
                        conn.execute(f"ALTER TABLE images ADD COLUMN {name} {kind}")
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc):
                            raise
            conn.execute("CREATE INDEX IF NOT EXISTS images_deck_page ON images(deck, page)")
            # One chat thread per history entry (deck id or image id); fork is the forked Claude session.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, entry TEXT NOT NULL, fork TEXT, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS threads_entry ON threads(entry, created DESC);
                CREATE TABLE IF NOT EXISTS messages (thread TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread, created);
            """)

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def ingest(self, paths, session, title="", caption="", deck=False, agent_session=None, agent_cwd=None):
        """Store every path; a PDF becomes one deck, and deck=True joins the whole call into one."""
        if not isinstance(session, str) or not session.strip() or len(session) > 200:
            raise ValueError("The project name must have 1 to 200 characters.")
        if not paths or len(paths) > 30:
            raise ValueError("Send between 1 and 30 images per call.")
        prepared = []
        written = []
        shared = uuid.uuid4().hex if deck else None
        try:
            for value in paths:
                path = Path(value).expanduser().resolve(strict=True)
                before = signature(path)
                group = shared or (uuid.uuid4().hex if is_pdf(path) else None)
                rows = []
                for image in load_pages(path):
                    if len(prepared) + len(rows) >= MAX_PAGES:
                        image.close()
                        raise ValueError(f"Send at most {MAX_PAGES} images or pages per call.")
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
                    rows.append(dict(id=item_id, session=session.strip(), title=(title or path.stem)[:300],
                        caption=caption[:2000], source=str(path), snapshot=str(snapshot),
                        width=image.width, height=image.height, created=time.time(), deck=group,
                        agent_session=agent_session, agent_cwd=agent_cwd))
                    image.close()
                after = signature(path)
                if before != after:
                    raise ValueError(f"The file is changing; try again: {path.name}")
                for row in rows:
                    row["signature"] = after
                prepared.extend(rows)
            decks = {}
            for row in prepared:
                if row["deck"]:
                    decks.setdefault(row["deck"], []).append(row)
            for rows in decks.values():
                for page, row in enumerate(rows, 1):
                    # The whole deck carries the title of its first file.
                    row.update(page=page, pages=len(rows), title=rows[0]["title"])
            for row in prepared:
                row.setdefault("page", None)
                row.setdefault("pages", None)
            with self.connect() as conn:
                conn.executemany("""INSERT INTO images
                    (id,session,title,caption,source,snapshot,width,height,created,signature,deck,page,pages,agent_session,agent_cwd)
                    VALUES (:id,:session,:title,:caption,:source,:snapshot,:width,:height,:created,:signature,:deck,:page,:pages,
                            :agent_session,:agent_cwd)""", prepared)
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return prepared

    def sessions(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                # entries: history entries (a deck counts once). snapshot: the newest image (SQLite takes it from the MAX row).
                "SELECT session,COUNT(*) AS count,COUNT(DISTINCT COALESCE(deck,id)) AS entries,MAX(created) AS updated,snapshot "
                "FROM images GROUP BY session ORDER BY updated DESC")]

    def history(self, session=None, limit=200, collapse=False, since=None):
        """Newest first. collapse=True lists each deck once, by its first slide; since keeps entries from that time on."""
        where, values = [], []
        if session is not None:
            where.append("session=?")
            values.append(session)
        if since is not None:
            where.append("created>=?")
            values.append(since)
        if collapse:
            where.append("(deck IS NULL OR page=1)")
        sql = "SELECT * FROM images" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created DESC LIMIT ?"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, (*values, limit))]

    def versions(self, session):
        """{entry: (n, total)}: position of each entry among the entries made from the same source file."""
        with self.connect() as conn:
            rows = conn.execute("SELECT COALESCE(deck, id) AS entry, source FROM images WHERE session=? "
                                "AND (deck IS NULL OR page=1) ORDER BY created", (session,)).fetchall()
        totals, seen, result = {}, {}, {}
        for row in rows:
            totals[row["source"]] = totals.get(row["source"], 0) + 1
        for row in rows:
            seen[row["source"]] = seen.get(row["source"], 0) + 1
            result[row["entry"]] = (seen[row["source"]], totals[row["source"]])
        return result

    def deck_pages(self, deck):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM images WHERE deck=? ORDER BY page", (deck,))]

    def get(self, item_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM images WHERE id=?", (item_id,)).fetchone()
            return dict(row) if row else None

    def latest_for_source(self, session, source):
        """Deck id (or image id) of the newest entry made from this source file."""
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(deck, id) AS entry FROM images WHERE session=? AND source=? ORDER BY created DESC LIMIT 1", (session, source)).fetchone()
            return row["entry"] if row else None

    def thread(self, entry):
        """Newest chat thread of a history entry, or None."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM threads WHERE entry=? ORDER BY created DESC LIMIT 1", (entry,)).fetchone()
            return dict(row) if row else None

    def new_thread(self, entry):
        row = dict(id=uuid.uuid4().hex, entry=entry, fork=None, created=time.time())
        with self.connect() as conn:
            conn.execute("INSERT INTO threads VALUES (:id, :entry, :fork, :created)", row)
        return row

    def set_fork(self, thread, fork):
        with self.connect() as conn:
            conn.execute("UPDATE threads SET fork=? WHERE id=?", (fork, thread))

    def add_message(self, thread, role, text):
        with self.connect() as conn:
            conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?)", (thread, role, text, time.time()))

    def messages(self, thread):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT role, text FROM messages WHERE thread=? ORDER BY created, rowid", (thread,))]

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
