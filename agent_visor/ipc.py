"""Private per-user Unix socket and serialized, on-demand startup."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from .storage import data_dir

MAX_REQUEST = 128 * 1024


def runtime_dir():
    override = os.environ.get("VISOR_RUNTIME_DIR")
    if override:
        directory = Path(override).expanduser()
    elif os.environ.get("XDG_RUNTIME_DIR"):
        directory = Path(os.environ["XDG_RUNTIME_DIR"]) / "agent-visor"
    else:
        directory = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agent-visor"
    return directory


def socket_path():
    path = str(runtime_dir() / "visor.sock")
    if len(os.fsencode(path)) > 103:
        raise RuntimeError("VISOR_RUNTIME_DIR is too long for a Unix socket path.")
    return path


def request(payload, timeout=20):
    packet = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(packet) > MAX_REQUEST:
        raise ValueError("The request is too large.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path())
        client.sendall(packet)
        stream = client.makefile("rb")
        line = stream.readline(2 * 1024 * 1024)
        if not line:
            raise ConnectionError("Visor closed the connection.")
        result = json.loads(line)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Visor error"))
        return result


def running():
    try:
        return request({"action": "status"}, timeout=1)
    except (OSError, ValueError, RuntimeError):
        return None


def ensure_running():
    state = running()
    if state:
        return state
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid():
        raise RuntimeError("The socket directory belongs to another user.")
    directory.chmod(0o700)
    with (directory / "startup.lock").open("a") as lock:
        # A bounded lock wait also handles several agents starting simultaneously.
        deadline = time.monotonic() + 18
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Another process is starting Visor. Try again.")
                time.sleep(0.1)
        state = running()
        if state:
            return state
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            raise RuntimeError("No graphical session. Run visor on your local desktop.")
        Path(socket_path()).unlink(missing_ok=True)
        logdir = data_dir()
        logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        logpath = logdir / "visor.log"
        env = os.environ.copy()
        root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        with logpath.open("ab") as log:
            child = subprocess.Popen([sys.executable, "-m", "agent_visor", "_serve"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                cwd=root, env=env, close_fds=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = running()
            if state:
                return state
            if child.poll() is not None:
                break
            time.sleep(0.1)
        if child.poll() is None:
            child.terminate()
        raise RuntimeError(f"Could not open the window. Check desktop access and {logpath}")
