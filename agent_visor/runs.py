"""Live runs: follow the progress file of a training or evaluation and read progress and metrics from its lines.
No GUI imports in this module.

A line counts when it is one of:
  [3/12] seed=0 acc=0.91            progress 3 of 12, plus key=value pairs
   25%|██▌   | 3/12 [00:10<00:30]   a tqdm bar (pairs in its postfix count too)
  {"step": 3, "total": 12, "acc": 0.91}
Numbers become metric curves over the step; anything else is kept as the latest value of that key."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time

REMOTE = re.compile(r"^([A-Za-z0-9][\w.@-]*):(/.+)$")  # host:/absolute/path, read over ssh
BRACKET = re.compile(r"^\s*\[(\d+)/(\d+)\]")
TQDM = re.compile(r"\|\s*(\d+)/(\d+) \[")
PAIR = re.compile(r"([A-Za-z_][\w./-]*)=([^\s,;\]]+)")
CHUNK = 4 * 1024 * 1024  # most bytes read per look
MAX_POINTS = 4000  # per curve; beyond it every other point is dropped
MAX_SERIES = 12
MAX_INFO = 24
DONE_GRACE = 60  # a finished run is followed this long, for lines written after the last step
QUIET_LIMIT = 12 * 3600  # an unfinished run that says nothing for this long is no longer followed
LOCKS = {}
SHARED = {}  # host: ssh options that reuse a connection the window keeps open to it (see mirror.py)


def empty():
    return {"step": None, "total": None, "lines": 0, "series": {}, "info": {}, "seen": [], "looked": False}


def fetch(source, offset, limit=CHUNK):
    """(size of the file, up to limit bytes of it from offset)."""
    remote = REMOTE.match(source)
    if not remote:
        path = Path(source)
        if not path.is_file():
            raise OSError(f"File not found: {source}")
        size = path.stat().st_size
        if size < offset or not limit:
            return size, b""
        with path.open("rb") as handle:
            handle.seek(offset)
            return size, handle.read(limit)
    host, path = remote.group(1), shlex.quote(remote.group(2))
    script = f"stat -c %s -- {path} && tail -c +{offset + 1} -- {path} | head -c {limit}"
    try:
        # BatchMode: never wait on a password or an authenticator prompt nobody would see.
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", *SHARED.get(host, ()), host, script],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=40)
    except subprocess.TimeoutExpired:
        raise OSError(f"No answer from {host}.") from None
    head, _, data = result.stdout.partition(b"\n")
    if result.returncode or not head.isdigit():
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OSError(detail[-1][:200] if detail else f"Cannot read {source}.")
    return int(head), data


def number(value):
    """A finite float, or None for anything that is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        value = float(value)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def read_line(line):
    """The values a line reports, or None when it is not a progress line."""
    if line.startswith("{"):
        try:
            values = json.loads(line)
        except ValueError:
            return None
        return values if isinstance(values, dict) else None
    match = BRACKET.match(line) or TQDM.search(line)
    if not match:
        return None
    return {"step": match.group(1), "total": match.group(2), **dict(PAIR.findall(line[match.end():]))}


def parse(data, chunk, chosen=()):
    """Add the complete lines of chunk to data; returns how many bytes they took.
    chosen: the keys to draw as curves (empty: every number)."""
    end = max(chunk.rfind(b"\n"), chunk.rfind(b"\r")) + 1  # tqdm ends its updates with \r
    for raw in re.split(rb"[\r\n]+", chunk[:end]):
        values = read_line(raw.decode("utf-8", "replace").strip())
        if values is None:
            continue
        data["lines"] += 1
        step, total = number(values.pop("step", None)), number(values.pop("total", None))
        if step is not None:
            data["step"] = step
        if total is not None:
            data["total"] = total
        x = step if step is not None else data["lines"]
        for key, value in values.items():
            key, y = str(key)[:60], number(value)
            curve = y is not None and (key in chosen if chosen else True)
            if curve and (key in data["series"] or len(data["series"]) < MAX_SERIES):
                points = data["series"].setdefault(key, [])
                points.append([x, y])
                if len(points) > MAX_POINTS:
                    del points[1::2]
            elif key in data["info"] or len(data["info"]) < MAX_INFO:
                data["info"][key] = str(value)[:200]
    return end


def remaining(data, now=None):
    """Seconds left at the pace measured while following, or None while there is nothing to measure."""
    seen, step, total = data["seen"], data["step"], data["total"]
    if step is None or not total or len(seen) < 2:
        return None
    (start, first), (last, latest) = seen[0], seen[-1]
    if latest <= first or last <= start:
        return None
    return max(0.0, (total - latest) * (last - start) / (latest - first) - ((now or time.time()) - last))


def finished(data):
    return bool(data["total"]) and data["step"] is not None and data["step"] >= data["total"]


def state(run):
    """running, waiting (nothing read yet), done, stopped (no longer followed) or error (the file cannot be read)."""
    if finished(run["data"]):
        return "done"
    if run["error"]:
        return "error"
    if run["closed"]:
        return "stopped"
    return "running" if run["data"]["lines"] else "waiting"


def advance(store, run_id):
    """Read what the file gained since the last look. Returns (run, changed); the run is another one
    when the file started again, which is a new launch writing to the same path and gets its own entry."""
    with LOCKS.setdefault(run_id, threading.Lock()):
        run = store.run(run_id)
        if run is None:
            return None, False
        now = time.time()
        fields = {}
        try:
            size, chunk = fetch(run["source"], run["offset"])
            if size < run["offset"]:
                run = store.restart_run(run)
                size, chunk = fetch(run["source"], 0)
            error = None
        except OSError as exc:
            chunk, error = b"", str(exc)
        data = run["data"]
        before, looked = data["step"], data["looked"]
        used = parse(data, chunk, run["metrics"])
        if not used and len(chunk) >= CHUNK:  # a huge stretch without a line end is not a log line
            used = len(chunk)
        if used:
            if data["step"] is not None and before is not None and data["step"] < before:
                data["seen"] = []
            # What the file already held at the first look says nothing about the pace.
            if data["step"] != before and looked:
                data["seen"] = (data["seen"] + [[now, data["step"]]])[-30:]
            fields.update(offset=run["offset"] + used, updated=now)
        if error is None and not looked:
            data["looked"] = True
        if used or data["looked"] != looked:
            fields["data"] = data
        if error != run["error"]:
            fields["error"] = error
        quiet = now - fields.get("updated", run["updated"])
        if quiet > (DONE_GRACE if finished(data) else QUIET_LIMIT):
            fields["closed"] = 1
        if fields:
            store.save_run(run["id"], **fields)
            run.update(fields)
        return run, bool(fields) or run["id"] != run_id


def summary(run):
    """What a run has read so far, for the command's answer."""
    data = run["data"]
    return {"run_id": run["id"], "step": data["step"], "total": data["total"], "metrics": list(data["series"]),
            "info": list(data["info"]), "error": run["error"]}
