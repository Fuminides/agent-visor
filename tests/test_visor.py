from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from agent_visor import chat
from agent_visor.cli import default_session
from agent_visor.storage import Store, page_count, signature

ROOT = Path(__file__).resolve().parents[1]


def graphical():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="av-store-")
        self.directory = Path(self.tmp.name)
        self.store = Store(self.directory / "data")
        self.source = self.directory / "with spaces ñ.png"
        Image.new("RGB", (200, 100), "red").save(self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_survives_source_replacement_and_deletion(self):
        first = self.store.ingest([str(self.source)], "study", "Before")[0]
        Image.new("RGB", (240, 120), "blue").save(self.source)
        second = self.store.ingest([str(self.source)], "study", "After")[0]
        self.source.unlink()
        with Image.open(first["snapshot"]) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 255))
            self.assertEqual(image.size, (200, 100))
        with Image.open(second["snapshot"]) as image:
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 255, 255))
        self.assertEqual([i["title"] for i in self.store.history("study")], ["After", "Before"])

    def test_bad_batch_rolls_back_images_and_database(self):
        broken = self.directory / "broken.png"
        broken.write_text("not an image")
        with self.assertRaises(ValueError):
            self.store.ingest([str(self.source), str(broken)], "study")
        self.assertEqual(self.store.history(), [])
        self.assertEqual(list(self.store.assets.iterdir()), [])

    def test_sessions_are_isolated_and_persistent(self):
        self.store.ingest([str(self.source)], "/one/project")
        self.store.ingest([str(self.source)], "/two/project")
        reopened = Store(self.store.directory)
        self.assertEqual(len(reopened.sessions()), 2)
        self.assertEqual(len(reopened.history("/one/project")), 1)
        self.assertEqual(len(reopened.history("/two/project")), 1)

    def test_thumbnail_and_original_dimensions(self):
        item = self.store.ingest([str(self.source)], "study")[0]
        with Image.open(Path(item["snapshot"]).with_suffix(".thumb.png")) as image:
            self.assertLessEqual(image.width, 124)
            self.assertLessEqual(image.height, 90)
        self.assertEqual((item["width"], item["height"]), (200, 100))

    def test_detects_replacement_even_with_same_size_and_timestamp(self):
        before = signature(self.source)
        stat = self.source.stat()
        replacement = self.directory / "replacement.png"
        replacement.write_bytes(self.source.read_bytes())
        os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        replacement.replace(self.source)
        self.assertNotEqual(signature(self.source), before)

    def test_settings_survive_restart(self):
        self.store.set_setting("follow", False)
        self.store.set_setting("geometry", "900x600+10+20")
        reopened = Store(self.store.directory)
        self.assertIs(reopened.setting("follow"), False)
        self.assertEqual(reopened.setting("geometry"), "900x600+10+20")

    def test_invalid_session_rejected(self):
        with self.assertRaises(ValueError):
            self.store.ingest([str(self.source)], " ")
        self.assertEqual(self.store.history(), [])

    def make_pdf(self, colors, name="deck.pdf"):
        path = self.directory / name
        pages = [Image.new("RGB", (160, 90), color) for color in colors]
        pages[0].save(path, save_all=True, append_images=pages[1:])
        return path

    def test_pdf_becomes_one_deck_in_page_order(self):
        pdf = self.make_pdf(["red", "lime", "blue"])
        self.assertEqual(page_count(pdf), 3)
        items = self.store.ingest([str(pdf), str(self.source)], "talk", caption="Charla")
        self.assertEqual(len(items), 4)
        deck = items[0]["deck"]
        self.assertTrue(deck)
        self.assertEqual([(i["page"], i["pages"], i["title"]) for i in items[:3]], [(1, 3, "deck"), (2, 3, "deck"), (3, 3, "deck")])
        self.assertIsNone(items[3]["deck"])
        pages = self.store.deck_pages(deck)
        with Image.open(pages[1]["snapshot"]) as image:
            self.assertEqual(max(image.size), 2400)
            red, green, blue, _ = image.getpixel((image.width // 2, image.height // 2))
            self.assertGreater(green, 200)
            self.assertLess(red + blue, 60)
        collapsed = self.store.history("talk", collapse=True)
        self.assertEqual([(i["deck"], i["page"]) for i in collapsed], [(None, None), (deck, 1)])
        self.assertEqual(len(self.store.history("talk")), 4)
        self.assertEqual(self.store.latest_for_source("talk", str(pdf)), deck)

    def test_deck_flag_joins_images_under_first_title(self):
        other = self.directory / "second.png"
        Image.new("RGB", (50, 50), "white").save(other)
        items = self.store.ingest([str(self.source), str(other)], "talk", deck=True)
        self.assertEqual(len({i["deck"] for i in items}), 1)
        self.assertEqual([i["page"] for i in items], [1, 2])
        self.assertEqual({i["title"] for i in items}, {"with spaces ñ"})
        self.assertEqual(len(self.store.history("talk", collapse=True)), 1)

    def test_bad_pdf_and_page_limit_roll_back(self):
        broken = self.directory / "broken.pdf"
        broken.write_bytes(b"%PDF-1.4 not really")
        with self.assertRaises(ValueError):
            page_count(broken)
        with self.assertRaises(ValueError):
            self.store.ingest([str(self.source), str(broken)], "talk")
        with patch("agent_visor.storage.MAX_PAGES", 2):
            with self.assertRaises(ValueError):
                self.store.ingest([str(self.make_pdf(["red", "lime", "blue"]))], "talk")
        self.assertEqual(self.store.history(), [])
        self.assertEqual(list(self.store.assets.iterdir()), [])

    def test_old_history_is_migrated(self):
        import sqlite3
        directory = self.directory / "old"
        directory.mkdir()
        with sqlite3.connect(directory / "history.sqlite3") as conn:
            conn.execute("""CREATE TABLE images (id TEXT PRIMARY KEY, session TEXT NOT NULL, title TEXT NOT NULL,
                caption TEXT NOT NULL, source TEXT NOT NULL, snapshot TEXT NOT NULL, width INTEGER NOT NULL,
                height INTEGER NOT NULL, created REAL NOT NULL, signature TEXT NOT NULL)""")
            conn.execute("INSERT INTO images VALUES ('a','s','t','','/x.png','/x.png',1,1,1.0,'sig')")
        store = Store(directory)
        self.assertEqual([(i["id"], i["deck"]) for i in store.history("s", collapse=True)], [("a", None)])
        self.assertEqual(store.latest_for_source("s", "/x.png"), "a")

    def test_agent_session_period_filter_and_threads(self):
        sid = "31cde43d-3856-47e7-990f-9d87748f1e6d"
        old = self.store.ingest([str(self.source)], "study")[0]
        with self.store.connect() as conn:
            conn.execute("UPDATE images SET created=? WHERE id=?", (time.time() - 7200, old["id"]))
        new = self.store.ingest([str(self.source)], "study", agent_session=sid, agent_cwd="/tmp")[0]
        self.assertEqual(self.store.get(new["id"])["agent_session"], sid)
        self.assertIsNone(self.store.get(old["id"])["agent_session"])
        self.assertEqual([i["id"] for i in self.store.history("study", since=time.time() - 3600)], [new["id"]])
        self.assertEqual(len(self.store.history("study")), 2)
        self.assertIsNone(self.store.thread(new["id"]))
        first = self.store.new_thread(new["id"])
        self.store.add_message(first["id"], "user", "¿Por qué?")
        self.store.add_message(first["id"], "assistant", "Porque sí.")
        self.store.set_fork(first["id"], "fork-1")
        self.assertEqual(self.store.thread(new["id"])["fork"], "fork-1")
        self.assertEqual([m["role"] for m in self.store.messages(first["id"])], ["user", "assistant"])
        time.sleep(.01)
        second = self.store.new_thread(new["id"])
        self.assertEqual(self.store.thread(new["id"])["id"], second["id"])
        self.assertEqual(self.store.messages(second["id"]), [])

    def test_no_session_basename_collision(self):
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=Path("/a/project")):
            self.assertEqual(default_session(), "/a/project")
        with patch.dict(os.environ, {"VISOR_SESSION": "custom"}):
            self.assertEqual(default_session(), "custom")


class ChatTests(unittest.TestCase):
    def test_first_question_forks_read_only_and_later_ones_continue(self):
        with patch("agent_visor.chat.claude_binary", return_value="claude"):
            first = chat.command("orig", None, "hola")
            later = chat.command("orig", "fork", "otra")
        self.assertEqual(first[:5], ["claude", "-p", "--resume", "orig", "--fork-session"])
        self.assertEqual(later[:4], ["claude", "-p", "--resume", "fork"])
        self.assertNotIn("--fork-session", later)
        for args in (first, later):
            self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")
            self.assertIn("--strict-mcp-config", args)
            self.assertEqual(args[-1], args[-1].strip())

    def test_stream_events(self):
        lines = [
            {"type": "system", "subtype": "init", "session_id": "abc"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": " Respuesta "}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Respuesta"},
        ]
        found = [event for line in lines for event in chat.events(json.dumps(line))]
        self.assertEqual(found, [("session", "abc"), ("tool", "Read /x.py"), ("text", "Respuesta"), ("done", "")])
        self.assertEqual(chat.events("not json"), [])
        failed = chat.events(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True}))
        self.assertEqual(failed[0][0], "error")

    def test_environment_and_session_folder(self):
        with patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x", "CLAUDE_CODE_USE_VERTEX": "1", "HOME_X": "y"}):
            env = chat.clean_env()
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertEqual(env["CLAUDE_CODE_USE_VERTEX"], "1")
        with tempfile.TemporaryDirectory(prefix="av-chat-") as directory:
            project = Path(directory) / "projects/-some-project"
            project.mkdir(parents=True)
            sid = "31cde43d-3856-47e7-990f-9d87748f1e6d"
            (project / f"{sid}.jsonl").write_text(json.dumps({"type": "meta"}) + "\n" + json.dumps({"cwd": directory}) + "\n")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": directory}):
                self.assertEqual(chat.session_cwd(sid, "/nonexistent"), directory)
                self.assertEqual(chat.session_cwd("00000000-0000-0000-0000-000000000000", directory), directory)


@unittest.skipUnless(graphical(), "No graphical desktop access")
class DesktopIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="av-it-")
        self.directory = Path(self.tmp.name)
        self.env = os.environ.copy()
        self.env.update(VISOR_DATA_DIR=str(self.directory / "data"), VISOR_RUNTIME_DIR=str(self.directory / "run"),
                        PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        self.source = self.directory / "result with spaces ñ.png"
        Image.new("RGB", (320, 240), "red").save(self.source)

    def cli(self, *args, check=True):
        completed = subprocess.run([sys.executable, "-m", "agent_visor", *map(str, args)],
                                   env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=35)
        if check and completed.returncode:
            self.fail(f"CLI {args}: {completed.stderr}\nLog: {self.log()}")
        return json.loads(completed.stdout) if check else completed

    def log(self):
        path = self.directory / "data/visor.log"
        return path.read_text() if path.exists() else ""

    def wait_for(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.15)
        self.fail(f"Timed out. Log: {self.log()}")

    def tearDown(self):
        try:
            self.cli("stop", "--json")
            self.wait_for(lambda: not (self.directory / "run/visor.sock").exists(), 6)
            self.assertNotIn("Traceback", self.log())
        finally:
            self.tmp.cleanup()

    def test_cli_full_lifecycle_watch_versions_restart_and_unicode(self):
        self.assertFalse(self.cli("status", "--json")["running"])
        first = self.cli("show", self.source, "--title", "Primera versión ñ", "--session", "Study", "--json")
        initial_pid = first["pid"]
        store = Store(self.directory / "data")
        first_snapshot = store.get(first["item_ids"][0])["snapshot"]
        time.sleep(.2)
        Image.new("RGB", (340, 250), "blue").save(self.source)
        self.wait_for(lambda: len(store.history("Study")) == 2)
        state = self.cli("status", "--json")
        self.assertEqual(state["pid"], initial_pid)
        self.assertNotEqual(state["current_id"], first["item_ids"][0])
        with Image.open(first_snapshot) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 255))
        second = self.cli("show", self.source, "--session", "Another project", "--json")
        self.assertEqual(second["pid"], initial_pid)
        self.assertEqual(second["session"], "Another project")
        self.cli("stop", "--json")
        self.wait_for(lambda: not (self.directory / "run/visor.sock").exists())
        restarted = self.cli("open", "--json")
        self.assertNotEqual(restarted["pid"], initial_pid)
        self.assertEqual(restarted["current_id"], second["item_ids"][0])
        self.assertEqual(len(self.cli("history", "--json")["items"]), 3)

    def test_simultaneous_agents_share_one_window(self):
        def show(index):
            return self.cli("show", self.source, "--session", f"Agent {index}", "--json")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(show, range(4)))
        self.assertEqual(len({result["pid"] for result in results}), 1)
        self.assertEqual(len(self.cli("history", "--json")["items"]), 4)

    def test_bad_input_does_not_open_a_window(self):
        bad = self.directory / "bad.png"
        bad.write_text("broken")
        completed = self.cli("show", self.source, bad, "--json", check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(self.cli("status", "--json")["running"])
        self.assertEqual(self.cli("history", "--json")["items"], [])

    def test_stale_socket_recovers_after_forced_exit(self):
        state = self.cli("show", self.source, "--session", "Recovery", "--json")
        os.kill(state["pid"], signal.SIGTERM)
        self.wait_for(lambda: not self.cli("status", "--json")["running"])
        new = self.cli("show", self.source, "--session", "Recovery", "--json")
        self.assertNotEqual(new["pid"], state["pid"])
        self.assertEqual(len(self.cli("history", "--json")["items"]), 2)


@unittest.skipUnless(graphical(), "No graphical desktop access")
class WindowBehaviorTests(unittest.TestCase):
    def test_paused_follow_keeps_selection_and_zoom_is_bounded(self):
        import tkinter as tk
        from agent_visor.app import Visor
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            path = Path(directory) / "image.png"
            Image.new("RGB", (1200, 900), "green").save(path)
            store = Store(Path(directory) / "data")
            initial = store.ingest([str(path)], "One")[0]
            root = tk.Tk()
            try:
                app = Visor(root, store)
                root.update()
                app.follow.set(False)
                newer = store.ingest([str(path)], "Two")
                response = app.dispatch({"action": "received", "items": newer})
                self.assertTrue(response["queued"])
                root.update()
                self.assertEqual(app.current["id"], initial["id"])
                self.assertTrue(app.new_button.winfo_ismapped())
                app.show_latest()
                self.assertEqual(app.current["id"], newer[0]["id"])
                app.zoom(10000)
                self.assertEqual(app.scale, 8)
                self.assertLessEqual(app.photo.width(), app.canvas.winfo_width() + 16)
                app.zoom(.0000001)
                self.assertEqual(app.scale, .02)
                app.set_fit()
                self.assertTrue(app.fit)
            finally:
                root.destroy()

    def test_presentation_turns_slides_and_keeps_page_on_update(self):
        import tkinter as tk
        from agent_visor.app import Visor
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            pdf = Path(directory) / "talk.pdf"
            pages = [Image.new("RGB", (160, 90), color) for color in ("red", "lime", "blue")]
            pages[0].save(pdf, save_all=True, append_images=pages[1:])
            single = Path(directory) / "single.png"
            Image.new("RGB", (100, 100), "white").save(single)
            store = Store(Path(directory) / "data")
            store.ingest([str(single)], "Talk")
            root = tk.Tk()
            try:
                app = Visor(root, store)
                deck = store.ingest([str(pdf)], "Talk")
                response = app.dispatch({"action": "received", "items": deck})
                root.update()
                self.assertFalse(response["queued"])
                self.assertEqual(app.current["id"], deck[0]["id"])
                self.assertEqual(len(app.items), 2)
                self.assertTrue(app.slide_bar.winfo_ismapped())
                self.assertEqual(app.slide_label.cget("text"), "Diapositiva 1 / 3")
                self.assertTrue(app.turn(1))
                self.assertTrue(app.turn(1))
                self.assertTrue(app.turn(1))
                self.assertEqual(app.current["page"], 3)
                regenerated = store.ingest([str(pdf)], "Talk")
                app.dispatch({"action": "received", "items": regenerated, "watched_id": deck[0]["deck"]})
                self.assertEqual(app.current["id"], regenerated[2]["id"])
                app.step(1)
                root.update()
                self.assertEqual(app.current["deck"], deck[0]["deck"])
                self.assertEqual(app.current["page"], 1)
                app.step(1)
                root.update()
                self.assertIsNone(app.current["deck"])
                self.assertFalse(app.turn(1))
                self.assertFalse(app.slide_bar.winfo_ismapped())
            finally:
                root.destroy()

    def test_chat_forks_once_then_continues_and_keys_ignore_typing(self):
        import tkinter as tk
        from agent_visor.app import Visor
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            directory = Path(directory)
            calls = directory / "calls.jsonl"
            fake = directory / "claude"
            fake.write_text(f"""#!{sys.executable}
import json, sys
open({str(calls)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
for event in [{{"type": "system", "subtype": "init", "session_id": "fork-123"}},
              {{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Read", "input": {{"file_path": "/a.py"}}}}]}}}},
              {{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "Es por el ruido."}}]}}}},
              {{"type": "result", "subtype": "success", "is_error": False}}]:
    print(json.dumps(event), flush=True)
""")
            fake.chmod(0o755)
            image = directory / "plot.png"
            Image.new("RGB", (300, 200), "white").save(image)
            plain = directory / "plain.png"
            Image.new("RGB", (300, 200), "black").save(plain)
            store = Store(directory / "data")
            store.ingest([str(plain)], "Chat")
            item = store.ingest([str(image)], "Chat", agent_session="31cde43d-3856-47e7-990f-9d87748f1e6d", agent_cwd=str(directory))[0]
            root = tk.Tk()
            try:
                with patch("agent_visor.chat.claude_binary", return_value=str(fake)):
                    app = Visor(root, store)
                    root.update()
                    self.assertEqual(app.current["id"], item["id"])
                    app.toggle_chat()
                    root.update()
                    self.assertTrue(app.chat.winfo_ismapped())
                    for question in ("¿Por qué baja?", "¿Y luego?"):
                        app.chat_input.insert("1.0", question)
                        app.send_or_stop()
                        deadline = time.monotonic() + 10
                        while app.runs and time.monotonic() < deadline:
                            root.update()
                            time.sleep(.02)
                        root.update()
                    self.assertEqual(app.thread["fork"], "fork-123")
                    self.assertEqual([m["role"] for m in store.messages(app.thread["id"])],
                                     ["user", "tool", "assistant", "user", "tool", "assistant"])
                    self.assertIn("Es por el ruido.", app.chat_log.get("1.0", "end"))
                    app.append_chat("assistant", "La **clave** es `x`.")
                    self.assertIn("La clave es x.", app.chat_log.get("1.0", "end"))
                    self.assertEqual(str(app.copy_button.cget("state")), "normal")
                    first, second = [json.loads(line) for line in calls.read_text().splitlines()]
                    self.assertIn("--fork-session", first)
                    self.assertEqual(first[first.index("--resume") + 1], "31cde43d-3856-47e7-990f-9d87748f1e6d")
                    self.assertNotIn("--fork-session", second)
                    self.assertEqual(second[second.index("--resume") + 1], "fork-123")
                    app.chat_input.focus_force()
                    root.update()
                    app.key(lambda: self.fail("shortcut fired while typing"))(None)
                    app.step(1)
                    root.update()
                    self.assertIsNone(app.current["agent_session"])
                    self.assertEqual(str(app.send_button.cget("state")), "disabled")
                    with store.connect() as conn:
                        conn.execute("UPDATE images SET created=? WHERE id=?", (time.time() - 7200, app.current["id"]))
                    app.set_period("hour")
                    self.assertEqual([i["id"] for i in app.items], [item["id"]])
                    app.set_period("all")
                    self.assertEqual(len(app.items), 2)
            finally:
                root.destroy()


class InstallerTests(unittest.TestCase):
    def test_install_is_repeatable_and_does_not_replace_other_commands(self):
        spec = importlib.util.spec_from_file_location("visor_install", ROOT / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix="av-install-") as directory:
            home = Path(directory)
            module.install(home, sys.executable)
            module.install(home, sys.executable)
            command = home / ".local/bin/visor"
            result = subprocess.run([str(command), "--version"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn("visor 0.1.0", result.stdout)
            for path in [".codex/skills/agent-visor", ".claude/skills/agent-visor"]:
                self.assertEqual((home / path).resolve(), ROOT / "skills/agent-visor")
            command.write_text("#!/bin/sh\necho another app\n")
            with self.assertRaises(RuntimeError):
                module.install(home, sys.executable)
            self.assertIn("another app", command.read_text())


if __name__ == "__main__":
    unittest.main()
