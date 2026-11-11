from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from agent_visor import chat, codex
from agent_visor.storage import Store

SID = "31cde43d-3856-47e7-990f-9d87748f1e6d"


def exact(peer, size):
    data = b""
    while len(data) < size:
        part = peer.recv(size - len(data))
        if not part:
            raise EOFError()
        data += part
    return data


def receive(peer):
    first, second = exact(peer, 2)
    assert first == 0x81 and second & 0x80  # clients must mask frames
    size = second & 127
    if size == 126:
        size = struct.unpack("!H", exact(peer, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", exact(peer, 8))[0]
    mask = exact(peer, 4)
    return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(exact(peer, size))))


def frame(peer, data, opcode=1, final=True):
    size = len(data)
    head = bytes(((0x80 if final else 0) | opcode, size)) if size < 126 else (
        bytes(((0x80 if final else 0) | opcode, 126)) + struct.pack("!H", size) if size < 65536 else
        bytes(((0x80 if final else 0) | opcode, 127)) + struct.pack("!Q", size))
    peer.sendall(head + data)


@contextmanager
def server(handler):
    with tempfile.TemporaryDirectory(prefix="visor-codex-") as directory:
        path = str(Path(directory) / "codex.sock")
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen()
        listener.settimeout(5)
        errors = []
        calls = []

        def worker():
            try:
                with listener.accept()[0] as peer:
                    peer.settimeout(5)
                    header = b""
                    while not header.endswith(b"\r\n\r\n"):
                        header += exact(peer, 1)
                    key = header.decode().split("Sec-WebSocket-Key: ")[1].split("\r\n")[0]
                    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                    peer.sendall(f"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
                    while True:
                        request = receive(peer)
                        calls.append(request)
                        if "id" in request:
                            result = {} if request["method"] == "initialize" else handler(request)
                            # Exercise fragmented messages, including a control frame between fragments.
                            data = json.dumps({"id": request["id"], "result": result}, ensure_ascii=False).encode()
                            frame(peer, data[:10], final=False)
                            frame(peer, b"", opcode=10)
                            frame(peer, data[10:], opcode=0)
            except (EOFError, ConnectionResetError, BrokenPipeError):
                pass
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            yield path, calls
        finally:
            listener.close()
            thread.join(6)
            if thread.is_alive():
                raise AssertionError("Mock Codex server did not shut down")
            if errors:
                raise errors[0]


class CodexTests(unittest.TestCase):
    def test_origin_uses_exact_thread_and_caller_home_with_claude_precedence(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": SID, "CODEX_SESSION_ID": "other", "CODEX_HOME": "/tmp/custom",
                                     "CLAUDE_CODE_SESSION_ID": "", "VISOR_CODEX_SOCKET": ""}):
            item = chat.origin()
            self.assertEqual((item["agent_provider"], item["agent_session"]), ("codex", SID))
            self.assertEqual(item["agent_socket"], "/tmp/custom/app-server-control/app-server-control.sock")
            with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SID}):
                self.assertEqual(chat.origin()["agent_provider"], "claude")
            with patch.dict(os.environ, {"CODEX_THREAD_ID": SID + "bad"}):
                self.assertEqual(chat.origin(), {})

    def test_origin_survives_storage_deck_run_restart_and_manual_track_update(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            image = directory / "figure.png"
            Image.new("RGB", (30, 20)).save(image)
            store = Store(directory / "data")
            origin = dict(agent_session=SID, agent_provider="codex", agent_socket="/tmp/codex.sock")
            images = store.ingest([str(image), str(image)], "Study", deck=True, **origin)
            for image in images:
                stored = store.get(image["id"])
                self.assertEqual({key: stored[key] for key in origin}, origin)
            run = store.track("/tmp/run.log", "Study", **origin)
            self.assertEqual(store.track("/tmp/run.log", "Study")["agent_provider"], "codex")
            restarted = store.restart_run(run)
            self.assertEqual({key: restarted[key] for key in origin}, origin)
            # Historical entries with no provider retain the Claude fork behavior.
            old = store.ingest([str(directory / "figure.png")], "Study", agent_session=SID)[0]
            self.assertIsNone(old["agent_provider"])

    def test_loaded_thread_pagination_and_no_reopening_stopped_session(self):
        def handler(request):
            if request["method"] == "thread/loaded/list":
                return {"data": [SID] if request["params"].get("cursor") else [],
                        "nextCursor": None if request["params"].get("cursor") else "next"}
            if request["method"] == "thread/read":
                return {"thread": {"id": SID, "status": {"type": "idle"}, "canAcceptDirectInput": True}}
            self.fail(request)
        with server(handler) as (path, calls):
            codex.available(dict(agent_socket=path, agent_session=SID))
        self.assertEqual([r["method"] for r in calls], ["initialize", "initialized", "thread/loaded/list", "thread/loaded/list", "thread/read"])
        with server(lambda r: {"data": [], "nextCursor": None}) as (path, calls):
            with self.assertRaisesRegex(RuntimeError, "no longer active"):
                list(codex.Question().events(dict(agent_socket=path, agent_session=SID), "Question"))
        self.assertFalse(any(r["method"] in ("thread/resume", "thread/start", "turn/start") for r in calls))

    def test_live_question_keeps_settings_and_excludes_earlier_replies(self):
        reads = 0
        def handler(request):
            nonlocal reads
            method = request["method"]
            if method == "thread/loaded/list":
                return {"data": [SID]}
            if method == "thread/read":
                return {"thread": {"status": {"type": "active"}, "canAcceptDirectInput": True}}
            if method == "turn/start":
                self.assertEqual(request["params"], {"threadId": SID, "input": [{"type": "text", "text": "Why?"}]})
                return {"turn": {"id": "turn-1"}}
            if method == "thread/turns/list":
                reads += 1
                blocks = [{"id": "old", "type": "agentMessage", "text": "Before the question"}]
                if reads > 1:
                    blocks.append({"id": "reply", "type": "agentMessage", "text": "Because ñ " + "x" * 70000})
                return {"data": [{"id": "turn-1", "status": "inProgress" if reads == 1 else "completed", "items": blocks}]}
            self.fail(request)
        with server(handler) as (path, calls):
            events = list(codex.Question().events(dict(agent_socket=path, agent_session=SID), "Why?"))
        self.assertEqual([kind for kind, _ in events], ["note", "text"])
        self.assertTrue(events[1][1].startswith("Because ñ"))
        self.assertFalse(any(r["method"].startswith("thread/resume") for r in calls))

    def test_live_prompt_describes_the_selected_version_without_fork_restrictions(self):
        item = dict(title="Plot", source="/plot.png", snapshot="/saved.png", deck="deck", page=2, pages=3)
        prompt = chat.prompt("Change its color", item, True, live=True)
        self.assertIn("slide 2 of 3", prompt)
        self.assertIn("/saved.png", prompt)
        self.assertNotIn("read and search", prompt)
        self.assertTrue(prompt.endswith("Change its color"))

    def test_detaching_does_not_interrupt_the_agent(self):
        observer = codex.Question()
        def handler(request):
            method = request["method"]
            if method == "thread/loaded/list":
                return {"data": [SID]}
            if method == "thread/read":
                return {"thread": {"status": {"type": "idle"}}}
            if method == "thread/turns/list":
                return {"data": []}
            if method == "turn/start":
                observer.terminate()
                return {"turn": {"id": "turn-1"}}
            self.fail(request)
        with server(handler) as (path, calls):
            events = list(observer.events(dict(agent_socket=path, agent_session=SID), "Hi"))
        self.assertEqual(events, [("note", "sent"), ("note", "detached")])
        self.assertFalse(any(r["method"] == "turn/interrupt" for r in calls))


    def test_figure_chat_forks_once_then_resumes_only_its_own_read_only_session(self):
        for existing in (None, "figure-fork"):
            reads = 0
            def handler(request):
                nonlocal reads
                method, params = request["method"], request["params"]
                if method in ("thread/fork", "thread/resume"):
                    self.assertEqual(method, "thread/resume" if existing else "thread/fork")
                    self.assertEqual(params["threadId"], existing or SID)
                    self.assertEqual(params["sandbox"], "read-only")
                    self.assertEqual(params["approvalPolicy"], "never")
                    self.assertEqual(params["config"], {"mcp_servers": {}, "features.apps": False})
                    return {"thread": {"id": "figure-fork"}}
                self.assertEqual(params["threadId"], "figure-fork")
                if method == "thread/goal/clear":
                    return {"cleared": False}
                if method == "turn/start":
                    self.assertEqual(params["sandboxPolicy"], {"type": "readOnly"})
                    self.assertEqual(params["approvalPolicy"], "never")
                    return {"turn": {"id": "figure-turn"}}
                if method == "thread/turns/list":
                    reads += 1
                    return {"data": [] if reads == 1 else [{"id": "figure-turn", "status": "completed",
                            "items": [{"id": "answer", "type": "agentMessage", "text": "The shaded area is uncertainty."}]}]}
                self.fail(request)
            with server(handler) as (path, calls):
                events = list(codex.Question(figure=True, fork=existing).events(dict(agent_socket=path, agent_session=SID), "Explain this"))
            self.assertEqual(events, [("session", "figure-fork"), ("text", "The shaded area is uncertainty.")])
            self.assertFalse(any(r["method"] == "thread/loaded/list" for r in calls))

    def test_stopping_figure_chat_interrupts_only_the_fork(self):
        observer = codex.Question(figure=True)
        def handler(request):
            method = request["method"]
            if method == "thread/fork":
                return {"thread": {"id": "figure-fork"}}
            self.assertEqual(request["params"]["threadId"], "figure-fork")
            if method == "thread/goal/clear":
                return {"cleared": False}
            if method == "thread/turns/list":
                return {"data": []}
            if method == "turn/start":
                observer.terminate()
                return {"turn": {"id": "figure-turn"}}
            if method == "turn/interrupt":
                self.assertEqual(request["params"]["turnId"], "figure-turn")
                return {}
            self.fail(request)
        with server(handler) as (path, calls):
            events = list(observer.events(dict(agent_socket=path, agent_session=SID), "Why?"))
        self.assertEqual(events, [("session", "figure-fork"), ("note", "figure_stopped")])
        self.assertEqual(sum(r["method"] == "turn/interrupt" for r in calls), 1)

    def test_modes_keep_separate_history_and_migrate_existing_codex_chats(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            image = directory / "figure.png"
            Image.new("RGB", (30, 20)).save(image)
            store = Store(directory / "data")
            codex_item = store.ingest([str(image)], "Study", agent_session=SID, agent_provider="codex")[0]
            claude_item = store.ingest([str(image)], "Study", agent_session=SID)[0]
            old_codex = store.new_thread(codex_item["id"])
            old_claude = store.new_thread(claude_item["id"])
            store.add_message(old_codex["id"], "user", "An original conversation question")
            with store.connect() as connection:
                connection.execute("ALTER TABLE threads DROP COLUMN mode")
            store = Store(directory / "data")
            self.assertEqual(store.thread(codex_item["id"], "live")["id"], old_codex["id"])
            self.assertEqual(store.thread(claude_item["id"], "figure")["id"], old_claude["id"])
            self.assertIsNone(store.thread(codex_item["id"], "figure"))
            fork = store.new_thread(codex_item["id"], "figure")
            store.add_message(fork["id"], "user", "A figure question")
            store = Store(directory / "data")
            self.assertEqual(store.thread(codex_item["id"], "figure")["id"], fork["id"])
            self.assertEqual(store.messages(store.thread(codex_item["id"], "live")["id"])[0]["text"],
                             "An original conversation question")
            self.assertEqual(store.messages(fork["id"])[0]["text"], "A figure question")


class CodexDesktopTests(unittest.TestCase):
    def test_panel_sends_to_origin_shows_reply_and_reconnects_without_new_thread(self):
        import time
        import tkinter as tk
        from agent_visor.app import Visor
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No graphical desktop access")
        reads = 0
        def handler(request):
            nonlocal reads
            method = request["method"]
            if method == "thread/loaded/list":
                return {"data": [SID]}
            if method == "thread/read":
                return {"thread": {"status": {"type": "idle"}, "canAcceptDirectInput": True}}
            if method == "turn/start":
                self.assertIn("Message sent from Visor to the original session", request["params"]["input"][0]["text"])
                self.assertIn("saved copy at", request["params"]["input"][0]["text"])
                return {"turn": {"id": "turn-1"}}
            if method == "thread/turns/list":
                reads += 1
                return {"data": [] if reads == 1 else [{"id": "turn-1", "status": "completed",
                        "items": [{"id": "reply", "type": "agentMessage", "text": "The confidence interval widens here."}]}]}
            self.fail(request)
        try:
            with tempfile.TemporaryDirectory() as directory, server(handler) as (path, calls), patch("agent_visor.codex.available"):
                directory = Path(directory)
                image = directory / "figure.png"
                Image.new("RGB", (300, 200), "white").save(image)
                store = Store(directory / "data")
                store.ingest([str(image)], "Study", agent_session=SID, agent_provider="codex", agent_socket=path)
                app = Visor(root, store)
                app.toggle_chat()
                deadline = time.monotonic() + 5
                while not app.codex_ready and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.01)
                self.assertTrue(app.codex_ready)
                self.assertIn("Connected to Codex", app.chat_status.cget("text"))
                app.chat_input.insert("1.0", "What happens here?")
                app.send_or_stop()
                deadline = time.monotonic() + 5
                while app.runs and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.01)
                self.assertFalse(app.runs)
                self.assertIn("The confidence interval widens here.", app.chat_log.get("1.0", "end"))
                self.assertEqual([m["role"] for m in store.messages(app.thread["id"])], ["user", "note", "assistant"])
                thread_id = app.thread["id"]
                app.new_thread()
                self.assertEqual(app.thread["id"], thread_id)
                app.copy_command()
                self.assertIn(SID, root.clipboard_get())
                self.assertIn("--remote", root.clipboard_get())
                with patch("agent_visor.codex.available", side_effect=RuntimeError("Session closed")):
                    app.new_thread()
                    deadline = time.monotonic() + 5
                    while "Session closed" not in app.chat_status.cget("text") and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.01)
                    self.assertFalse(app.codex_ready)
                    self.assertEqual(str(app.send_button.cget("state")), "disabled")
                app.close()
        finally:
            try:
                if root.winfo_exists():
                    root.destroy()
            except tk.TclError:
                pass


    def test_toggle_restores_each_history_and_draft_while_the_other_mode_is_answering(self):
        import time
        import tkinter as tk
        from agent_visor.app import Visor
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No graphical desktop access")
        original_started, original_finish = threading.Event(), threading.Event()
        def answers(observer, item, prompt):
            if observer.figure:
                yield "session", "figure-fork"
                yield "text", "Figure answer"
            else:
                original_started.set()
                original_finish.wait(5)
                yield "text", "Original answer"
        def pump(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                root.update()
                time.sleep(.01)
            self.assertTrue(predicate())
        try:
            with tempfile.TemporaryDirectory() as directory, patch("agent_visor.codex.available"), patch.object(codex.Question, "events", answers):
                directory = Path(directory)
                image = directory / "figure.png"
                Image.new("RGB", (300, 200), "white").save(image)
                store = Store(directory / "data")
                item = store.ingest([str(image)], "Study", agent_session=SID, agent_provider="codex")[0]
                app = Visor(root, store)
                app.toggle_chat()
                pump(lambda: app.codex_ready)
                app.chat_input.insert("1.0", "Original draft")
                app.mode_buttons["figure"].invoke()
                pump(lambda: app.codex_ready)
                self.assertEqual(app.chat_input.get("1.0", "end-1c"), "")
                app.chat_input.insert("1.0", "Figure draft")
                app.mode_buttons["live"].invoke()
                pump(lambda: app.codex_ready)
                self.assertEqual(app.chat_input.get("1.0", "end-1c"), "Original draft")
                app.send_or_stop()
                pump(original_started.is_set)
                original = app.thread["id"]
                app.mode_buttons["figure"].invoke()
                pump(lambda: app.codex_ready)
                self.assertEqual(app.chat_input.get("1.0", "end-1c"), "Figure draft")
                self.assertNotIn("Original draft", app.chat_log.get("1.0", "end"))
                app.send_or_stop()
                figure = app.thread["id"]
                pump(lambda: figure not in app.runs)
                self.assertIn("Figure answer", app.chat_log.get("1.0", "end"))
                self.assertEqual(app.thread["fork"], "figure-fork")
                self.assertIn(original, app.runs)
                app.copy_command()
                self.assertIn("figure-fork", root.clipboard_get())
                original_finish.set()
                pump(lambda: original not in app.runs)
                self.assertNotIn("Original answer", app.chat_log.get("1.0", "end"))
                app.set_chat_mode("live")
                pump(lambda: app.codex_ready)
                self.assertEqual(app.thread["id"], original)
                self.assertIn("Original answer", app.chat_log.get("1.0", "end"))
                self.assertNotIn("Figure answer", app.chat_log.get("1.0", "end"))
                app.set_chat_mode("figure")
                pump(lambda: app.codex_ready)
                self.assertEqual(app.thread["id"], figure)
                app.set_theme("sky")
                pump(lambda: app.codex_ready)
                self.assertEqual(app.chat_mode, "figure")
                self.assertEqual(store.setting(f"chat_mode:{item['id']}"), "figure")
                app.new_thread()
                self.assertNotEqual(app.thread["id"], figure)
                self.assertEqual(store.thread(item["id"], "live")["id"], original)
                app.close()
        finally:
            original_finish.set()
            try:
                if root.winfo_exists():
                    root.destroy()
            except tk.TclError:
                pass


if __name__ == "__main__":
    unittest.main()
