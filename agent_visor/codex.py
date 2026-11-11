"""Ask a loaded local Codex thread or discuss a figure in a separate read-only fork.

Codex's Unix control socket carries JSON-RPC over WebSocket (RFC 6455). Keeping
this small local transport here avoids adding a networking dependency to Visor.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import threading
import time

MAX_MESSAGE = 16 * 1024 * 1024


def control_socket():
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return os.environ.get("VISOR_CODEX_SOCKET") or str(home / "app-server-control/app-server-control.sock")


class Connection:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(5)
        self.sequence = 0
        try:
            self.socket.connect(path)
            key = base64.b64encode(os.urandom(16)).decode()
            self.socket.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                                 f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                                 "Sec-WebSocket-Version: 13\r\n\r\n").encode())
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                header.extend(self.exact(1))
                if len(header) > 8192:
                    raise RuntimeError("Invalid Codex WebSocket handshake.")
            lines = header.decode("ascii").split("\r\n")
            headers = {name.lower(): value.strip() for line in lines[1:] if ":" in line
                       for name, value in [line.split(":", 1)]}
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if lines[0].split()[1] != "101" or headers.get("sec-websocket-accept", "").strip() != accept:
                raise RuntimeError("Codex rejected the WebSocket connection.")
            self.call("initialize", {"clientInfo": {"name": "agent_visor", "version": "0.1.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.socket.close()

    def exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.socket.recv(size - len(data))
            if not part:
                raise ConnectionError("Codex closed the connection.")
            data.extend(part)
        return bytes(data)

    def frame(self, data, opcode=1):
        mask = os.urandom(4)
        size = len(data)
        header = bytes((0x80 | opcode, 0x80 | size)) if size < 126 else (
            bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", size) if size < 65536 else
            bytes((0x80 | opcode, 0xFF)) + struct.pack("!Q", size))
        self.socket.sendall(header + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(data)))

    def send(self, data):
        self.frame(json.dumps(data, ensure_ascii=False).encode())

    def receive(self):
        data = bytearray()
        while True:
            first, second = self.exact(2)
            opcode, size = first & 15, second & 127
            if size == 126:
                size = struct.unpack("!H", self.exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.exact(8))[0]
            if size + len(data) > MAX_MESSAGE or second & 128 or first & 0x70:
                raise RuntimeError("Invalid or oversized Codex WebSocket frame.")
            payload = self.exact(size)
            if opcode == 8:
                raise ConnectionError("Codex closed the connection.")
            if opcode == 9:
                self.frame(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise RuntimeError("Unexpected Codex WebSocket frame.")
            data.extend(payload)
            if first & 0x80:
                return json.loads(data)

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = self.receive()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", "Codex request failed."))
                return message.get("result", {})
            # Do not answer approvals: the existing Codex UI owns them.
        raise TimeoutError("Codex did not answer in time.")

    def require_loaded(self, thread_id):
        cursor = None
        while True:
            result = self.call("thread/loaded/list", {"cursor": cursor})
            if thread_id in result["data"]:
                thread = self.call("thread/read", {"threadId": thread_id})["thread"]
                if thread.get("canAcceptDirectInput") is False:
                    raise RuntimeError("This Codex session cannot accept messages from Visor.")
                if thread.get("status", {}).get("type") == "notLoaded":
                    break
                return thread
            cursor = result.get("nextCursor")
            if not cursor:
                break
        raise RuntimeError("The Codex session is no longer active. Open it in Codex, then reconnect.")


def available(item, figure=False, fork=None):
    with Connection(item.get("agent_socket") or control_socket()) as client:
        if figure:
            client.call("thread/read", {"threadId": fork or item["agent_session"]})
        else:
            client.require_loaded(item["agent_session"])


FIGURE_INSTRUCTIONS = ("This is Visor's separate figure chat. Use the inherited conversation only as context. "
                       "Answer questions about the selected figure or run; do not continue earlier tasks or goals. "
                       "Do not edit files, run experiments, or take actions in external services. "
                       "Use read-only inspection when needed. Reply briefly in the user's language.")


def figure_options():
    return {"sandbox": "read-only", "approvalPolicy": "never", "excludeTurns": True,
            "developerInstructions": FIGURE_INSTRUCTIONS,
            "config": {"mcp_servers": {}, "features.apps": False}}


class Question:
    """Stop only a figure fork; for the original chat, detach and leave its agent working."""
    def __init__(self, figure=False, fork=None):
        self.stopped = threading.Event()
        self.figure = figure
        self.fork = fork

    def terminate(self):
        self.stopped.set()

    def events(self, item, prompt):
        with Connection(item.get("agent_socket") or control_socket()) as client:
            thread_id = item["agent_session"]
            if self.figure:
                if self.fork == thread_id:
                    raise RuntimeError("The figure chat must be separate from the original session.")
                method = "thread/resume" if self.fork else "thread/fork"
                params = dict(figure_options(), threadId=self.fork or thread_id)
                if not self.fork:
                    params["deferGoalContinuation"] = True
                forked = client.call(method, params)["thread"]["id"]
                if forked == thread_id:
                    raise RuntimeError("Codex did not create a separate figure chat.")
                thread_id = self.fork = forked
                yield "session", thread_id
                # A fork can inherit an ongoing goal. Clear only the fork's copy, so
                # asking about a plot never starts background work from that goal.
                client.call("thread/goal/clear", {"threadId": thread_id})
            else:
                client.require_loaded(thread_id)
            if self.stopped.is_set():
                yield "note", "figure_stopped" if self.figure else "detached"
                return
            previous = client.call("thread/turns/list", {"threadId": thread_id, "limit": 1, "itemsView": "full"})
            seen = {block["id"] for turn in previous["data"] for block in turn.get("items", [])
                    if block.get("type") == "agentMessage"}
            # turn/start on a loaded thread also accepts input while a turn is active.
            # No model, permission, sandbox or cwd overrides: keep the original session's settings.
            params = {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]}
            if self.figure:
                params.update(approvalPolicy="never", sandboxPolicy={"type": "readOnly"})
            result = client.call("turn/start", params)
            turn_id = result["turn"]["id"]
            if not self.figure:
                yield "note", "sent"
            # Read only this turn. Polling avoids taking over subscriptions/approval handling
            # from the original client. The paginated API works with Codex's newer history store.
            while not self.stopped.is_set():
                result = client.call("thread/turns/list", {"threadId": thread_id, "limit": 1, "itemsView": "full"})
                turn = next((turn for turn in result["data"] if turn["id"] == turn_id), None)
                cursor = result.get("nextCursor")
                while turn is None and cursor:
                    result = client.call("thread/turns/list", {"threadId": thread_id, "limit": 1,
                                                              "itemsView": "full", "cursor": cursor})
                    turn = next((turn for turn in result["data"] if turn["id"] == turn_id), None)
                    cursor = result.get("nextCursor")
                if turn:
                    blocks = turn.get("items", [])
                    for index, block in enumerate(blocks):
                        # The last item of an active turn may still be streaming. Wait
                        # until it has a successor or the turn finishes to save its full text.
                        if turn["status"] == "inProgress" and index == len(blocks) - 1:
                            continue
                        if block.get("type") == "agentMessage" and block.get("text") and block["id"] not in seen:
                            seen.add(block["id"])
                            yield "text", block["text"]
                    if turn["status"] != "inProgress":
                        if turn.get("error"):
                            yield "error", turn["error"].get("message", str(turn["error"]))
                        elif turn["status"] == "interrupted":
                            yield "note", "interrupted"
                        return
                else:
                    client.require_loaded(thread_id)
                self.stopped.wait(1)
            if self.figure:
                client.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            yield "note", "figure_stopped" if self.figure else "detached"
