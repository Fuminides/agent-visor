"""Show on this computer what agents show on another one. No GUI imports in this module.

`visor follow HOST` makes the window here keep an ssh connection to HOST and run `visor feed` there, which prints
every new history entry as one JSON line, figure included. The connection always goes from the computer with the
window to the computer with the agents (a laptop reaches a workstation, rarely the other way round), and after a
break the feed continues from the last entry received.

A line is {"kind": "figure", "project", "label", "cursor", "rows": [one per slide, with its PNG in base64]} or
{"kind": "run", "project", "label", "cursor", "title", "source", "metrics"}; an empty line only says the
connection is alive."""
from __future__ import annotations

import base64
from itertools import groupby
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

from .ipc import runtime_dir
from .runs import REMOTE
from .storage import conversation

HOST = re.compile(r"[A-Za-z0-9][\w.@-]*")  # a name from the ssh config; never something ssh would read as an option
CATCH_UP = 20  # most history entries sent at once: to a computer that follows for the first time, or after a long break
BEAT = 10  # seconds between the lines that keep a quiet connection checked
RETRY = 15  # seconds before a lost connection is tried again
ASKED = "visor: asked"  # left on ssh's error output by the dialog (askpass.py): a refused answer is not a network failure
ROW = ("id", "title", "caption", "source", "created", "deck", "page", "pages")


def sharing(host):
    """ssh options that send every call to HOST through the feed's connection, so a password is asked once and
    the readings of a live run need none. A host whose ssh config already shares its connections keeps its own way."""
    try:
        config = subprocess.run(["ssh", "-G", host], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        config = ""
    if re.search(r"^controlpath (?!none)\S", config, re.I | re.M):
        return []
    runtime_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    return ["-o", f"ControlPath={runtime_dir()}/ssh-%C"]


def askpass():
    """The program ssh runs to ask the user (ssh wants one executable, not a command line): opens askpass.py's dialog."""
    path = runtime_dir() / "askpass"
    root = Path(__file__).resolve().parent.parent
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(f'#!/bin/sh\nPYTHONPATH={shlex.quote(str(root))} exec {shlex.quote(sys.executable)} -m agent_visor _askpass "$1"\n')
    path.chmod(0o700)
    return str(path)


def command(host, cursor, shared=()):
    """The ssh call that prints HOST's feed from cursor on; shared: what sharing(host) gave. What ssh has to ask
    (a password, a code, a new host's key) it asks through SSH_ASKPASS, which the caller points at askpass()."""
    there = "~/.local/bin/visor feed" + "".join(f" --{key} {int(value)}" for key, value in sorted(cursor.items()))
    return ["ssh", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15", *(["-o", "ControlMaster=auto", *shared] if shared else []),
            host, there]


def feed(store, cursor, out, follow=True, pause=1.0):
    """Write to out a line for each history entry stored after cursor ({"images": rowid, "runs": rowid}), and with
    follow=True go on with every new one until out is closed."""
    with store.connect() as conn:
        # At most the newest entries, counted whole: a presentation is never cut in the middle.
        newest = conn.execute("SELECT MIN(rowid) FROM images WHERE COALESCE(deck,id) IN (SELECT COALESCE(deck,id) "
                              "FROM images GROUP BY 1 ORDER BY MAX(rowid) DESC LIMIT ?)", (CATCH_UP,)).fetchone()[0]
        images = max(cursor.get("images") or 0, (newest or 1) - 1)
        runs = cursor.get("runs")
        if runs is None:  # a first time: the runs still being followed, and what came after them
            last = conn.execute("SELECT MAX(rowid) FROM runs").fetchone()[0] or 0
            runs = (conn.execute("SELECT MIN(rowid) FROM runs WHERE closed=0").fetchone()[0] or last + 1) - 1

    def send(line=None):
        out.write((json.dumps(line, ensure_ascii=False) if line else "") + "\n")
        out.flush()

    send()
    beat = time.monotonic()
    while True:
        with store.connect() as conn:
            figures = [dict(row) for row in conn.execute("SELECT rowid AS cursor, * FROM images WHERE rowid>? ORDER BY rowid", (images,))]
            started = [dict(row) for row in conn.execute("SELECT rowid AS cursor, * FROM runs WHERE rowid>? ORDER BY rowid", (runs,))]
        for _, rows in groupby(figures, key=lambda row: row["deck"] or row["id"]):
            rows = list(rows)
            images = rows[-1]["cursor"]
            project, label = conversation(rows[0]["session"])
            try:
                slides = [{**{key: row[key] for key in ROW}, "png": base64.b64encode(Path(row["snapshot"]).read_bytes()).decode()}
                          for row in rows]
            except OSError:
                continue  # its copy is gone from this computer
            send({"kind": "figure", "project": project, "label": label, "cursor": {"images": images}, "rows": slides})
        for run in started:
            runs = run["cursor"]
            if not REMOTE.match(run["source"]):  # a file on a third computer may be out of the follower's reach
                project, label = conversation(run["session"])
                send({"kind": "run", "project": project, "label": label, "cursor": {"runs": runs}, "title": run["title"],
                      "source": run["source"], "metrics": json.loads(run["metrics"])})
        if not follow:
            return
        if figures or started:
            beat = time.monotonic()
        elif time.monotonic() - beat > BEAT:
            send()
            beat = time.monotonic()
        time.sleep(pause)


def receive(store, host, line):
    """Store what a line of HOST's feed carries. Returns (action for the window, its items, cursor after the line);
    the items are empty when the entry was here already."""
    entry = json.loads(line)
    # Its own project in the list: the figures of a folder on HOST are not mixed with those of the same folder here.
    session = (f"{entry['project']} @ {host}" + (f" / {entry['label']}" if entry["label"] else ""))[:200]
    if entry["kind"] == "run":  # followed from here, by reading the file over ssh like any `visor track host:/path`
        return "track", [store.track(f"{host}:{entry['source']}", session, entry["title"], entry["metrics"])], entry["cursor"]
    rows = [dict(row, session=session, source=f"{host}:{row['source']}", png=base64.b64decode(row["png"])) for row in entry["rows"]]
    return "received", store.adopt(rows), entry["cursor"]
