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

from agent_visor.cli import default_session
from agent_visor.storage import Store, signature

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

    def test_no_session_basename_collision(self):
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=Path("/a/project")):
            self.assertEqual(default_session(), "/a/project")
        with patch.dict(os.environ, {"VISOR_SESSION": "custom"}):
            self.assertEqual(default_session(), "custom")


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
