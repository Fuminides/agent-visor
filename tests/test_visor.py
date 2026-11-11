from __future__ import annotations

import concurrent.futures
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from agent_visor import chat, mirror, runs
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


def settle(app):
    """Wait until the figure being read in the background is on the stage."""
    deadline = time.monotonic() + 10
    while app.loading and time.monotonic() < deadline:
        app.root.update()
        time.sleep(.005)
    app.root.update()


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
        self.store.add_message(first["id"], "user", "Why?")
        self.store.add_message(first["id"], "assistant", "Because.")
        self.store.set_fork(first["id"], "fork-1")
        self.assertEqual(self.store.thread(new["id"])["fork"], "fork-1")
        self.assertEqual([m["role"] for m in self.store.messages(first["id"])], ["user", "assistant"])
        time.sleep(.01)
        second = self.store.new_thread(new["id"])
        self.assertEqual(self.store.thread(new["id"])["id"], second["id"])
        self.assertEqual(self.store.messages(second["id"]), [])

    def test_versions_count_only_the_same_source_file(self):
        other = self.directory / "other.png"
        Image.new("RGB", (40, 40), "white").save(other)
        first = self.store.ingest([str(self.source)], "study")[0]
        unrelated = self.store.ingest([str(other)], "study")[0]
        second = self.store.ingest([str(self.source)], "study")[0]
        versions = self.store.versions("study")
        self.assertEqual(versions[first["id"]], (1, 2))
        self.assertEqual(versions[second["id"]], (2, 2))
        self.assertEqual(versions[unrelated["id"]], (1, 1))

    def test_conversations_are_filed_under_their_project(self):
        from agent_visor.storage import conversation
        self.assertEqual(conversation("Study / comparison"), ("Study", "comparison"))
        self.assertEqual(conversation("Demo"), ("Demo", ""))
        repo, tree = self.directory / "atlas", self.directory / "atlas-wt-look"
        (repo / ".git").mkdir(parents=True)
        (repo / "paper/figures").mkdir(parents=True)
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/atlas-wt-look\n")
        self.assertEqual(conversation(str(repo)), ("atlas", ""))
        self.assertEqual(conversation(str(repo / "paper/figures")), ("atlas", "paper/figures"))
        self.assertEqual(conversation(str(tree)), ("atlas", "wt-look"))  # a worktree joins its main repository
        self.assertEqual(conversation(str(self.directory / "gone/folder")), ("folder", ""))
        deck = self.directory / "deck.pdf"
        pages = [Image.new("RGB", (160, 90), "white") for _ in range(3)]
        pages[0].save(deck, save_all=True, append_images=pages[1:])
        self.store.ingest([str(self.source)], "Study / comparison")
        self.store.ingest([str(deck)], "Study / comparison")
        row = self.store.sessions()[0]
        self.assertEqual((row["count"], row["entries"]), (4, 2))  # the deck is one entry
        self.assertEqual(row["snapshot"], self.store.history("Study / comparison")[0]["snapshot"])

    def test_no_session_basename_collision(self):
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=Path("/a/project")):
            self.assertEqual(default_session(), "/a/project")
        with patch.dict(os.environ, {"VISOR_SESSION": "custom"}):
            self.assertEqual(default_session(), "custom")


class MirrorTests(unittest.TestCase):
    def test_feed_carries_figures_presentations_and_runs_to_another_store(self):
        with tempfile.TemporaryDirectory(prefix="av-mirror-") as directory:
            directory = Path(directory)
            there, here = Store(directory / "there"), Store(directory / "here")
            plot, slide = directory / "plot.png", directory / "slide.png"
            Image.new("RGB", (300, 200), "green").save(plot)
            Image.new("RGB", (160, 90), "white").save(slide)
            there.ingest([str(slide), str(slide)], "Study / comparison", "Slides", deck=True)
            there.ingest([str(plot)], "Study / comparison", "Plot", "It drops.", agent_session="31cde43d-3856-47e7-990f-9d87748f1e6d")
            log = directory / "run.log"
            log.write_text("[1/4] loss=0.5\n")
            there.track(str(log), "Study / comparison", "Training", ["loss"])
            there.track("peanut:/data/run.log", "Study / comparison", "On a third computer")
            out = io.StringIO()
            mirror.feed(there, {}, out, follow=False)
            lines = [line for line in out.getvalue().splitlines() if line]
            self.assertEqual([json.loads(line)["kind"] for line in lines], ["figure", "figure", "run"])
            cursor = {}
            for line in lines:
                action, items, at = mirror.receive(here, "box", line)
                self.assertTrue(items)
                cursor.update(at)
            # Filed as its own project, with where it came from; a presentation keeps its slides.
            figure, second, first = here.history()
            self.assertEqual({row["session"] for row in here.history()}, {"Study @ box / comparison"})
            self.assertEqual((figure["title"], figure["caption"], figure["source"]), ("Plot", "It drops.", f"box:{plot}"))
            self.assertEqual((figure["width"], figure["height"], figure["agent_session"]), (300, 200, None))
            self.assertTrue(Path(figure["snapshot"]).with_suffix(".thumb.png").is_file())
            self.assertEqual([(row["title"], row["page"], row["pages"]) for row in here.deck_pages(first["deck"])],
                             [("Slides", 1, 2), ("Slides", 2, 2)])
            # The run is followed from here, over ssh; the one on a third computer is left out.
            self.assertEqual([(run["title"], run["source"], run["metrics"]) for run in here.runs()], [("Training", f"box:{log}", ["loss"])])
            # A line that arrives twice adds nothing, and the feed continues after what was received.
            self.assertEqual([mirror.receive(here, "box", line)[1] for line in lines[:2]], [[], []])
            mirror.receive(here, "box", lines[2])
            self.assertEqual((len(here.history()), len(here.runs())), (3, 1))
            there.ingest([str(plot)], "Other", "Later")
            out = io.StringIO()
            mirror.feed(there, cursor, out, follow=False)
            self.assertEqual([json.loads(line)["rows"][0]["title"] for line in out.getvalue().splitlines() if line], ["Later"])
            # After a long break only the newest entries come, and a presentation is never cut.
            with patch.object(mirror, "CATCH_UP", 2):
                out = io.StringIO()
                mirror.feed(there, {}, out, follow=False)
            self.assertEqual([json.loads(line)["rows"][0]["title"] for line in out.getvalue().splitlines() if '"figure"' in line], ["Plot", "Later"])
            # The id names files on this computer, so one that is not an id is refused.
            entry = json.loads(lines[1])
            entry["rows"][0]["id"] = "../../outside"
            with self.assertRaises(ValueError):
                mirror.receive(here, "box", json.dumps(entry))
            self.assertEqual(mirror.command("box", {"runs": 3, "images": 7})[-2:], ["box", "~/.local/bin/visor feed --images 7 --runs 3"])

    def test_one_connection_serves_the_feed_and_the_readings_of_its_runs(self):
        with tempfile.TemporaryDirectory(prefix="av-") as directory, patch.dict(os.environ, {"VISOR_RUNTIME_DIR": directory}):
            config = subprocess.CompletedProcess([], 0, stdout="hostname box\ncontrolmaster false\n")
            with patch("agent_visor.mirror.subprocess.run", return_value=config):
                shared = mirror.sharing("box")
            self.assertEqual(shared, ["-o", f"ControlPath={directory}/ssh-%C"])
            # The feed opens the connection (asking through the dialog, so not in batch mode); a run's readings reuse it.
            feed = mirror.command("box", {}, shared)
            self.assertEqual(feed[-5:-2], ["ControlMaster=auto", *shared])
            self.assertNotIn("BatchMode=yes", feed)
            calls = []
            with patch.dict(runs.SHARED, {"box": shared}), patch("agent_visor.runs.subprocess.run", side_effect=lambda command, **kwargs: (
                    calls.append(command), subprocess.CompletedProcess(command, 0, stdout=b"3\nabc", stderr=b""))[1]):
                self.assertEqual(runs.fetch("box:/data/run.log", 0), (3, b"abc"))
                runs.fetch("peanut:/data/run.log", 0)
            self.assertEqual(calls[0][:8], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", *shared, "box"])
            self.assertEqual(calls[1][:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "peanut"])
            # A host whose ssh config already shares connections keeps its own way.
            config.stdout = "hostname box\ncontrolpath /home/me/.ssh/cm-%C\n"
            with patch("agent_visor.mirror.subprocess.run", return_value=config):
                self.assertEqual(mirror.sharing("box"), [])
            script = Path(mirror.askpass())
            self.assertTrue(os.access(script, os.X_OK))
            self.assertIn("-m agent_visor _askpass", script.read_text())


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="av-run-")
        self.directory = Path(self.tmp.name)
        self.store = Store(self.directory / "data")
        self.log = self.directory / "run.log"

    def tearDown(self):
        self.tmp.cleanup()

    def follow(self, text, **options):
        self.log.write_text(text)
        return runs.advance(self.store, self.store.track(str(self.log), "study", **options)["id"])[0]

    def test_log_lines_tqdm_bars_and_json_become_progress_curves_and_latest_values(self):
        run = self.follow("starting up\n[1/4] seed=0 acc=0.5 phase=warm\n"
                          " 50%|█████     | 2/4 [00:10<00:10,  5.0s/it, acc=0.6]\r"
                          '{"step": 3, "total": 4, "acc": 0.7, "loss": NaN, "best": true}\n[4/4] acc=0.9', metrics=["acc"])
        data = run["data"]
        self.assertEqual((data["step"], data["total"], data["lines"]), (3, 4, 3))  # the unfinished last line waits
        self.assertEqual(data["series"], {"acc": [[1, .5], [2, .6], [3, .7]]})
        self.assertEqual(data["info"], {"seed": "0", "phase": "warm", "loss": "nan", "best": "True"})
        self.assertEqual(run["offset"], len(self.log.read_bytes()) - len("[4/4] acc=0.9"))
        self.assertEqual(runs.state(run), "running")
        with self.log.open("a") as handle:
            handle.write("\n")
        run, changed = runs.advance(self.store, run["id"])
        self.assertTrue(changed)
        self.assertEqual((run["data"]["step"], runs.state(run)), (4, "done"))
        self.assertEqual(runs.advance(self.store, run["id"])[1], False)
        # Without a choice every number is a curve; a file with nothing readable keeps waiting.
        self.assertEqual(set(self.follow("[1/2] a=1 b=2 c=x\n", metrics=[])["data"]["series"]), {"a", "b"})
        other = self.store.track(str(self.directory / "data/visor.log"), "study")
        (self.directory / "data/visor.log").write_text("nothing here\n{broken\n[1, 2]\n")
        self.assertEqual(runs.state(runs.advance(self.store, other["id"])[0]), "waiting")

    def test_time_left_comes_from_the_pace_seen_while_following(self):
        run = self.follow("".join(f"[{k}/10] loss=1\n" for k in range(1, 5)))
        self.assertEqual(run["data"]["seen"], [])  # what the file already held says nothing about the pace
        self.assertIsNone(runs.remaining(run["data"]))
        for step, moment in ((5, 1000.0), (6, 1010.0), (7, 1020.0)):
            with self.log.open("a") as handle:
                handle.write(f"[{step}/10] loss=1\n")
            with patch("agent_visor.runs.time.time", return_value=moment):
                run, _ = runs.advance(self.store, run["id"])
        self.assertEqual(run["data"]["seen"], [[1000.0, 5], [1010.0, 6], [1020.0, 7]])
        self.assertEqual(runs.remaining(run["data"], 1020.0), 30)  # 3 steps left at 10 s each
        self.assertEqual(runs.remaining(run["data"], 1025.0), 25)
        self.assertEqual(runs.remaining(run["data"], 9999.0), 0)

    def test_relaunch_into_the_same_file_is_a_new_entry_and_notes_stay_with_their_run(self):
        first = self.follow("[1/3] acc=0.1\n[2/3] acc=0.2\n", title="Sweep")
        self.assertEqual(self.store.track(str(self.log), "study")["id"], first["id"])
        self.store.add_note(first["id"], "first try")
        self.log.write_text("[1/9] acc=0.4\n")
        second, changed = runs.advance(self.store, first["id"])
        self.assertTrue(changed)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual((second["title"], second["data"]["total"], second["notes"]), ("Sweep", 9, []))
        kept = self.store.run(first["id"])
        self.assertEqual((kept["closed"], kept["data"]["step"], kept["notes"][0][1]), (1, 2, "first try"))
        self.assertEqual(self.store.track(str(self.log), "study")["id"], second["id"])
        self.assertEqual([run["id"] for run in self.store.runs("study")], [second["id"], first["id"]])
        self.assertEqual([run["id"] for run in self.store.runs(following=True)], [second["id"]])
        for index in range(25):
            self.store.add_note(second["id"], f"note {index}")
        self.assertEqual([note for _, note in self.store.run(second["id"])["notes"]][::19], ["note 5", "note 24"])
        # A conversation that only holds runs is listed too, and runs count as its entries.
        Image.new("RGB", (20, 20), "red").save(self.directory / "a.png")
        self.store.ingest([str(self.directory / "a.png")], "figures")
        self.assertEqual({row["session"]: (row["entries"], row["snapshot"] is None) for row in self.store.sessions()},
                         {"study": (2, True), "figures": (1, False)})
        with self.assertRaises(ValueError):
            self.store.track(str(self.log), " ")

    def test_finished_silent_and_unreadable_runs(self):
        run = self.follow("[2/2] acc=1\n")
        self.assertEqual((runs.state(run), run["closed"]), ("done", 0))  # lines after the last step are still read
        self.store.save_run(run["id"], updated=time.time() - runs.DONE_GRACE - 1)
        self.assertEqual(runs.advance(self.store, run["id"])[0]["closed"], 1)
        slow = self.directory / "slow.log"
        slow.write_text("[1/5] acc=1\n")
        run = runs.advance(self.store, self.store.track(str(slow), "study")["id"])[0]
        self.store.save_run(run["id"], updated=time.time() - 3600)
        self.assertEqual(runs.advance(self.store, run["id"])[0]["closed"], 0)
        slow.unlink()
        run, changed = runs.advance(self.store, run["id"])
        self.assertEqual((changed, runs.state(run)), (True, "error"))
        self.assertIn("File not found", run["error"])
        self.store.save_run(run["id"], updated=time.time() - runs.QUIET_LIMIT - 1)
        run = runs.advance(self.store, run["id"])[0]
        self.assertEqual(run["closed"], 1)
        self.assertEqual(self.store.track(str(slow), "study")["closed"], 0)  # asking again follows it again
        self.assertEqual(runs.advance(self.store, "missing"), (None, False))

    def test_a_file_on_another_machine_is_read_over_ssh_without_prompts(self):
        calls = []

        def ssh(args, **options):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, b"30\n[3/12] acc=0.5\n", b"")
        with patch("agent_visor.runs.subprocess.run", ssh):
            self.assertEqual(runs.fetch("peanut:/data/it's a run.log", 12), (30, b"[3/12] acc=0.5\n"))
        self.assertEqual(calls[0][:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "peanut"])
        self.assertIn("tail -c +13 -- '/data/it'\"'\"'s a run.log'", calls[0][6])
        failed = subprocess.CompletedProcess([], 255, b"", b"warning\nssh: connect to host peanut port 22: Connection timed out\n")
        with patch("agent_visor.runs.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(OSError, "Connection timed out"):
                runs.fetch("peanut:/data/run.log", 0)
        with patch("agent_visor.runs.subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 40)):
            with self.assertRaisesRegex(OSError, "No answer from peanut"):
                runs.fetch("peanut:/data/run.log", 0)
        # Only a plain host name is taken as a machine: nothing that ssh would read as an option.
        with patch("agent_visor.runs.subprocess.run", side_effect=AssertionError("ssh must not run")):
            for source in ("-oProxyCommand=x:/tmp/a", "peanut:relative.log", "C:\\run.log"):
                with self.assertRaisesRegex(OSError, "File not found"):
                    runs.fetch(source, 0)


class IconTests(unittest.TestCase):
    def test_mark_is_antialiased_without_a_halo(self):
        from agent_visor.app import ACCENT, BG, visor_mark
        mark = visor_mark(40, ACCENT, BG, BG).convert("RGB")
        colours = {colour for _, colour in mark.getcolors(maxcolors=40 * 40)}
        self.assertGreater(len(colours), 10)  # blended edge pixels, not just two flat colours
        background = mark.getpixel((0, 0))
        lightest = max(sum(pixel) for pixel in colours)
        self.assertLessEqual(lightest, sum((255, 255, 255)))
        darkest = min(colours, key=sum)
        self.assertGreaterEqual(sum(darkest), sum(mark.getpixel((20, 6))))  # nothing darker than the disc: no ringing halo
        self.assertEqual("#%02x%02x%02x" % background, BG)
        icon = visor_mark(256, ACCENT, BG)
        self.assertEqual(icon.getpixel((0, 0))[3], 0)  # the app icon is transparent outside the disc

    def test_every_theme_keeps_text_and_focus_readable(self):
        from agent_visor.app import THEMES, SWATCHES

        def luminance(colour):
            parts = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            red, green, blue = [c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4 for c in parts]
            return .2126 * red + .7152 * green + .0722 * blue

        def ratio(first, second):
            high, low = sorted((luminance(first), luminance(second)), reverse=True)
            return (high + .05) / (low + .05)

        self.assertEqual(set(THEMES), set(SWATCHES))
        for name, theme in THEMES.items():
            self.assertEqual(set(theme), set(THEMES["sand"]), name)
            for surface in ("BG", "PANEL", "CARD", "SELECTED", "FIELD", "STAGE"):
                for text in ("INK", "MUTED"):
                    self.assertGreaterEqual(ratio(theme[text], theme[surface]), 4.5, f"{name}: {text} on {surface}")
            for surface in ("PANEL", "SELECTED"):
                self.assertGreaterEqual(ratio(theme["ACCENT_TEXT"], theme[surface]), 4.5, f"{name}: accent text on {surface}")
            self.assertGreaterEqual(ratio(theme["ERROR"], theme["PANEL"]), 4.5, name)
            self.assertGreaterEqual(ratio(theme["ON_ACCENT"], theme["ACCENT"]), 4.5, name)
            self.assertGreaterEqual(ratio(theme["BG"], theme["INK"]), 4.5, name)  # a chosen pill
            for surface in ("PANEL", "FIELD"):  # the question box's focus edge
                self.assertGreaterEqual(ratio(theme["ACCENT"], theme[surface]), 3, f"{name}: focus on {surface}")


class LanguageTests(unittest.TestCase):
    def test_both_languages_have_the_same_text_keys(self):
        from agent_visor import i18n
        self.assertEqual(set(i18n.TEXT["en"]), set(i18n.TEXT["es"]))
        self.assertEqual(i18n.text("es", "slide", page=2, pages=5), "Diapositiva 2 / 5")
        self.assertEqual(i18n.text("xx", "send"), "Send")


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

    def test_a_run_is_described_to_the_fork_by_what_visor_read(self):
        run = {"kind": "run", "title": "PPO", "source": "peanut:/data/run.log", "dump": "/data/runs/abc.json"}
        for first in (True, False):
            text = chat.prompt("How is it going?", run, first)
            self.assertIn("peanut:/data/run.log", text)
            self.assertIn("/data/runs/abc.json", text)
            self.assertTrue(text.endswith("How is it going?"))
        self.assertIn("visor track", chat.prompt("?", run, True))

    def test_point_is_marked_for_the_fork(self):
        with tempfile.TemporaryDirectory(prefix="av-point-") as directory:
            snapshot = Path(directory) / "fig.png"
            Image.new("RGB", (400, 300), "white").save(snapshot)
            marked, zoom = chat.mark_point(snapshot, 100, 50, Path(directory) / "points")

            def red_near(path, x, y, reach):
                with Image.open(path) as image:
                    pixels = image.convert("RGB").load()
                    return any(pixels[i, j][0] > 200 and pixels[i, j][1] < 110
                               for i in range(max(0, x - reach), min(image.width, x + reach))
                               for j in range(max(0, y - reach), min(image.height, y + reach)))
            self.assertTrue(red_near(marked, 100, 50, 14))
            self.assertFalse(red_near(marked, 300, 220, 30))
            with Image.open(zoom) as image:
                self.assertEqual(max(image.size), 768)
                factor = 768 / 220
            self.assertTrue(red_near(zoom, round(100 * factor), round(50 * factor), 26))
            item = dict(title="t", source="/s.png", snapshot=str(snapshot), width=400, height=300)
            text = chat.prompt("What is this?", item, True, dict(x=100, y=50, marked=marked, zoom=zoom))
            self.assertIn("x=100, y=50", text)
            self.assertIn(marked, text)
            self.assertIn(zoom, text)
            # A framed region: its edge is red in the copy, and the zoom is the region with a little around it.
            framed, zoom = chat.mark_region(snapshot, (200, 100, 320, 160), Path(directory) / "points")
            self.assertTrue(red_near(framed, 200, 130, 4) and red_near(framed, 320, 130, 4) and red_near(framed, 260, 100, 4))
            self.assertFalse(red_near(framed, 260, 130, 20))  # the inside is left as it was
            self.assertFalse(red_near(framed, 60, 250, 30))
            with Image.open(zoom) as image:
                self.assertEqual(image.size, (768, round(84 * 768 / 144)))  # 120 x 60 plus 12 px on every side
            asked = chat.prompt("What is in here?", item, True, dict(box=(200, 100, 320, 160), marked=framed, zoom=zoom))
            self.assertIn("from x=200, y=100 to x=320, y=160", asked)
            self.assertIn("50 to 80 % of the width", asked)
            self.assertIn(framed, asked)
            self.assertIn(zoom, asked)
            self.assertTrue(asked.endswith("What is in here?"))
            self.assertTrue(text.endswith("What is this?"))
        with patch("agent_visor.chat.claude_binary", return_value="claude"):
            args = chat.command("orig", None, "pregunta", ["/data/images", "/data/points"])
        self.assertEqual(args[-1], "pregunta")
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--add-dir"], ["/data/images", "/data/points"])

    def test_stream_events(self):
        lines = [
            {"type": "system", "subtype": "init", "session_id": "abc"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": " Respuesta "}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Respuesta"},
        ]
        found = [event for line in lines for event in chat.events(json.dumps(line))]
        self.assertEqual(found, [("session", "abc"), ("tool", "Read x.py"), ("text", "Respuesta"), ("done", "")])
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
        first = self.cli("show", self.source, "--title", "First version ñ", "--session", "Study", "--json")
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

    def test_track_follows_a_file_and_says_what_it_understood(self):
        log = self.directory / "train ñ.log"
        missing = self.cli("track", self.directory / "nothing.log", "--json", check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("File not found", missing.stderr)
        self.assertFalse(self.cli("status", "--json")["running"])
        log.write_text("[1/4] reward=10.5 loss=0.9 seed=3\n")
        first = self.cli("track", log, "--title", "PPO ñ", "--metrics", "reward,loss", "--note", "Baseline", "--session", "RL", "--json")
        self.assertEqual((first["step"], first["total"], first["metrics"], first["info"], first["error"]),
                         (1, 4, ["reward", "loss"], ["seed"], None))
        self.assertEqual((first["session"], first["queued"], first["current_id"]), ("RL", False, first["run_id"]))
        store = Store(self.directory / "data")
        with log.open("a") as handle:
            handle.write("[2/4] reward=12 loss=0.7 seed=3\n")
        self.wait_for(lambda: store.run(first["run_id"])["data"]["step"] == 2)  # read by the window on its own
        again = self.cli("track", log, "--note", "Second note", "--session", "RL", "--json")
        self.assertEqual((again["run_id"], again["pid"], again["step"]), (first["run_id"], first["pid"], 2))
        self.assertEqual([note for _, note in store.run(first["run_id"])["notes"]], ["Baseline", "Second note"])
        self.assertIn("step 2/4", self.cli("track", log, "--session", "RL", check=False).stdout)
        empty = self.directory / "empty.log"
        empty.write_text("")
        self.assertIn("no progress line yet", self.cli("track", empty, "--session", "RL", check=False).stdout)
        self.cli("stop", "--json")
        self.wait_for(lambda: not (self.directory / "run/visor.sock").exists())
        with log.open("a") as handle:
            handle.write("[3/4] reward=13 loss=0.6 seed=3\n")
        self.cli("open", "--json")  # an open run is followed again when the window comes back
        self.wait_for(lambda: store.run(first["run_id"])["data"]["step"] == 3)

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
                settle(app)
                app.zoom(10000)
                self.assertEqual(app.scale, 8)
                self.assertLessEqual(app.photo.width(), app.canvas.winfo_width() + 16)
                app.zoom(.0000001)
                self.assertEqual(app.scale, .02)
                app.set_fit()
                self.assertTrue(app.fit)
            finally:
                root.destroy()

    def test_followed_computer_shows_its_figures_here_and_continues_after_a_break(self):
        import tkinter as tk
        from agent_visor.app import Visor
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            directory = Path(directory)
            image = directory / "plot.png"
            Image.new("RGB", (300, 200), "green").save(image)
            there, store = Store(directory / "there"), Store(directory / "data")
            there.ingest([str(image)], "Study", "First")

            # The other computer is a second history on this one: the same feed, without ssh in between.
            def local(host, cursor, shared=()):
                return [sys.executable, "-m", "agent_visor", "feed", *(part for key, value in cursor.items() for part in (f"--{key}", str(value)))]

            def until(condition):
                deadline = time.monotonic() + 20
                while not condition() and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                self.assertTrue(condition())
            root = tk.Tk()
            try:
                with patch.object(mirror, "command", local), patch.object(mirror, "RETRY", 1), \
                        patch.dict(os.environ, {"VISOR_DATA_DIR": str(directory / "there"), "PYTHONPATH": str(ROOT)}):
                    app = Visor(root, store)
                    self.assertEqual(app.dispatch({"action": "follow", "host": "box"})["mirrors"], {"box": "connecting"})
                    until(lambda: app.current and app.current["title"] == "First")
                    self.assertEqual((app.session, app.dispatch({"action": "status"})["mirrors"]), ("Study @ box", {"box": "connected"}))
                    there.ingest([str(image)], "Study", "Second")
                    until(lambda: app.current["title"] == "Second")
                    # The connection drops: it comes back by itself and brings only what is new.
                    app.feeds["box"].kill()
                    there.ingest([str(image)], "Study", "Third")
                    until(lambda: app.current["title"] == "Third")
                    self.assertEqual([item["title"] for item in store.history()], ["Third", "Second", "First"])
                    self.assertEqual(store.setting("mirrors"), ["box"])  # followed again the next time the window opens
                    app.dispatch({"action": "follow", "host": "box", "off": True})
                    until(lambda: app.feeds["box"].poll() is not None)
                    self.assertEqual((app.mirrors, store.setting("mirrors")), ({}, []))
                    # A password that was asked for and did not connect is not asked again every few seconds.
                    attempts = []

                    def refused(host, cursor, shared=()):
                        attempts.append(host)
                        return ["sh", "-c", f"echo '{mirror.ASKED}' >&2; echo 'Permission denied (publickey,password).' >&2; exit 255"]
                    with patch.object(mirror, "command", refused):
                        app.dispatch({"action": "follow", "host": "box"})
                        until(lambda: app.mirrors["box"].get("paused"))
                        time.sleep(1.5)  # longer than the pause before another try
                        self.assertEqual(attempts, ["box"])
                        self.assertEqual(app.dispatch({"action": "status"})["mirrors"]["box"],
                                         "Permission denied (publickey,password). (run `visor follow box` to try again)")
                    there.ingest([str(image)], "Study", "Fourth")
                    app.dispatch({"action": "follow", "host": "box"})  # the user says to try again
                    until(lambda: app.current["title"] == "Fourth")
                    app.dispatch({"action": "follow", "host": "box", "off": True})
                    until(lambda: app.feeds["box"].poll() is not None)
            finally:
                root.destroy()

    def test_ssh_asks_through_a_dialog(self):
        import tkinter as tk
        from agent_visor.askpass import Dialog
        root = tk.Tk()
        try:
            dialog = Dialog(root, "javier@box's password: ", language="es")
            root.update()
            self.assertEqual(dialog.entry.cget("show"), "•")  # what is typed is not shown
            self.assertEqual(dialog.accept_button.cget("text"), "Conectar")
            dialog.entry.insert(0, "secret")
            dialog.accept()
            self.assertEqual(dialog.answer, "secret")
            for child in root.winfo_children():
                child.destroy()
            # A computer never seen before: ssh wants "yes" typed; the dialog offers it as a button.
            dialog = Dialog(root, "The authenticity of host 'box' can't be established.\nAre you sure you want to continue connecting (yes/no/[fingerprint])?")
            root.update()
            self.assertIsNone(dialog.entry)
            self.assertEqual(dialog.accept_button.cget("text"), "Yes")
            dialog.accept()
            self.assertEqual(dialog.answer, "yes")
            for child in root.winfo_children():
                child.destroy()
            dialog = Dialog(root, "Password: ")
            dialog.cancel()
            self.assertIsNone(dialog.answer)  # cancelling answers nothing: ssh gives up
        finally:
            root.destroy()

    def test_figure_is_read_in_the_background_and_the_last_choice_wins(self):
        import tkinter as tk
        from agent_visor.app import Visor
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            wide, tall = Path(directory) / "wide.png", Path(directory) / "tall.png"
            Image.new("RGB", (1200, 300), "green").save(wide)
            Image.new("RGB", (300, 1200), "blue").save(tall)
            store = Store(Path(directory) / "data")
            store.ingest([str(wide)], "One")
            store.ingest([str(tall)], "One")
            root = tk.Tk()
            try:
                app = Visor(root, store)
                settle(app)
                self.assertEqual(app.original.size, (300, 1200))
                older, newer = app.items[1], app.items[0]
                app.select(older)
                app.select(newer)
                app.select(older)  # chosen one after another, before any of them is read
                self.assertTrue(app.loading)  # select returns without waiting for the figure
                self.assertIsNone(app.original)
                self.assertEqual(app.title_label.cget("text"), "wide")
                settle(app)
                self.assertEqual(app.original.size, (1200, 300))
                self.assertEqual(app.photo.width(), round(1200 * app.scale))
                self.assertTrue(app.zoom_bar.winfo_ismapped())
                # The figure arrives already scaled for the stage, and the same view is not scaled again.
                scaled = app.scaled[1]
                app.draw()
                self.assertIs(app.scaled[1], scaled)
                app.zoom(2)
                self.assertIsNot(app.scaled[1], scaled)
                # A snapshot that cannot be read leaves the stage empty instead of waiting for ever.
                Path(newer["snapshot"]).unlink()
                app.select(newer)
                settle(app)
                self.assertIsNone(app.loading)
                self.assertIsNone(app.original)
            finally:
                root.destroy()

    def test_live_run_page_updates_scrolls_and_sits_in_the_history(self):
        import tkinter as tk
        from agent_visor.app import Visor
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            directory = Path(directory)
            image = directory / "plot.png"
            Image.new("RGB", (300, 200), "white").save(image)
            log = directory / "run.log"
            log.write_text("".join(f"[{k}/40] reward={k * 2.5} loss={1 / k:.3f} lr=0.001 a=1 b=2 c=3 d=4 phase=train\n" for k in range(1, 11)))
            store = Store(directory / "data")
            figure = store.ingest([str(image)], "RL")[0]
            root = tk.Tk()
            try:
                app = Visor(root, store)
                root.geometry("1000x640")
                root.update()
                # The test reads the file itself, so the reader thread is given nothing to do (figures still load in theirs).
                reader = patch.object(app, "run_worker")
                reader.start()
                self.addCleanup(reader.stop)
                run = runs.advance(store, store.track(str(log), "RL", "PPO run", agent_session="31cde43d-3856-47e7-990f-9d87748f1e6d")["id"])[0]
                store.add_note(run["id"], "Looks stable so far.")
                response = app.dispatch({"action": "track", "items": [store.run(run["id"])]})
                root.update()
                self.assertFalse(response["queued"])
                self.assertEqual((app.current["id"], [item["id"] for item in app.items]), (run["id"], [run["id"], figure["id"]]))
                self.assertIn(run["id"], app.following)
                app.draw()
                shown = [app.canvas.itemcget(item, "text") for item in app.canvas.find_all() if app.canvas.type(item) == "text"]
                for expected in ("Running", "25 %", "10 / 40", "reward", "25", "Looks stable so far.", "Estimating the time left…"):
                    self.assertIn(expected, shown)
                self.assertEqual(len(app.run_plots), 7)
                self.assertFalse(app.zoom_bar.winfo_ismapped())
                self.assertEqual(app.title_label.cget("text"), "PPO run")
                row = app.cards[run["id"]]
                self.assertEqual(row.meta.cget("text"), "25 %")
                # A new line reaches the row and the page without rebuilding the list.
                with log.open("a") as handle:
                    handle.write("[20/40] reward=80 loss=0.01\n")
                changed = runs.advance(store, run["id"])[0]
                app.dispatch({"action": "run", "id": run["id"]})
                app.draw()
                self.assertIs(app.cards[run["id"]], row)
                self.assertTrue(row.meta.cget("text").startswith("50 %"))
                self.assertEqual(app.current["data"]["step"], 20)
                # Pointing at a curve shows the value of its nearest point; the wheel scrolls the page within its length.
                chart = app.run_plots[0]
                where = type("Event", (), {"x": round(chart["across"](10)), "y": round(chart["plot"][1] + 5)})()
                app.point_at(where)
                self.assertIn("25  @  10", [app.canvas.itemcget(item, "text") for item in app.canvas.find_withtag("hover")
                                           if app.canvas.type(item) == "text"])
                app.point_at(None)
                self.assertFalse(app.canvas.find_withtag("hover"))
                self.assertGreater(app.run_height, app.canvas.winfo_height())
                for _ in range(40):
                    app.wheel(.9, None)
                self.assertEqual(app.run_scroll, app.run_height - app.canvas.winfo_height())
                for _ in range(40):
                    app.wheel(1.1, None)
                self.assertEqual(app.run_scroll, 0)
                # It lives in the history with the figures, in every theme and language, and has its own questions.
                app.set_theme("dark")
                app.set_language("es")
                root.update()
                self.assertEqual(app.current["id"], run["id"])
                self.assertIn("EN VIVO", [w.cget("text") for w in app.cards[run["id"]].winfo_children()[1].winfo_children()][-1])
                app.toggle_chat()
                root.update()
                self.assertEqual(str(app.send_button.cget("state")), "normal")
                self.assertIn("ejecución", app.chat_log.get("1.0", "end"))
                app.step(1)
                settle(app)
                self.assertEqual(app.current["id"], figure["id"])
                self.assertTrue(app.zoom_bar.winfo_ismapped())
                app.watch()
                app.step(-1)
                self.assertEqual(app.current["id"], run["id"])
                # Finished: the row says so; a relaunch into the same file takes its place on screen.
                with log.open("a") as handle:
                    handle.write("[40/40] reward=100 loss=0.001\n")
                runs.advance(store, run["id"])
                app.dispatch({"action": "run", "id": run["id"]})
                self.assertTrue(app.cards[run["id"]].meta.cget("text").startswith("Terminado"))
                log.write_text("[1/8] reward=1\n")
                relaunched = runs.advance(store, run["id"])[0]
                app.dispatch({"action": "run", "id": relaunched["id"]})
                root.update()
                self.assertEqual((app.current["id"], len(app.items)), (relaunched["id"], 3))
                app.dispatch({"action": "run_end", "id": relaunched["id"], "started": run["id"], "failed": True})
                self.assertNotIn(run["id"], app.following)
                # A conversation that only holds a run appears in the browser without a thumbnail.
                only = store.track(str(log), "Only runs")
                app.dispatch({"action": "track", "items": [only]})
                app.toggle_browser()
                root.update()
                self.assertEqual({session for session, _ in app.browser_rows}, {"RL", "Only runs"})
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
                self.assertEqual(app.slide_label.cget("text"), "Slide 1 / 3")
                self.assertTrue(app.turn(1))
                self.assertTrue(app.turn(1))
                self.assertTrue(app.turn(1))
                self.assertEqual(app.current["page"], 3)
                app.toggle_chat()
                app.set_language("es")
                root.update()
                self.assertEqual(app.current["page"], 3)
                self.assertEqual(app.slide_label.cget("text"), "Diapositiva 3 / 3")
                self.assertEqual(app.period_buttons["all"].cget("text"), "Todo")
                self.assertEqual(app.chat.winfo_manager(), "pack")  # mapping after a rebuild is timing-dependent
                self.assertEqual(Store(Path(directory) / "data").setting("language"), "es")
                app.set_language("en")
                app.toggle_chat()
                root.update()
                self.assertEqual(app.slide_label.cget("text"), "Slide 3 / 3")
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
              {{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "It is the noise."}}]}}}},
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
                    for question in ("Why does it drop?", "And then?"):
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
                    self.assertIn("It is the noise.", app.chat_log.get("1.0", "end"))
                    app.append_chat("assistant", "The **key** is `x`.")
                    self.assertIn("The key is x.", app.chat_log.get("1.0", "end"))
                    self.assertEqual(str(app.copy_button.cget("state")), "normal")
                    width, height = app.canvas.winfo_width(), app.canvas.winfo_height()
                    app.canvas.event_generate("<Button-3>", x=width // 2, y=height // 2)
                    app.canvas.event_generate("<B3-Motion>", x=width // 2 + 2, y=height // 2 + 1)  # a click is never perfectly still
                    app.canvas.event_generate("<ButtonRelease-3>", x=width // 2 + 2, y=height // 2 + 1)
                    root.update()
                    self.assertEqual(len(app.point), 2)
                    self.assertTrue(abs(app.point[0] - 150) <= 2 and abs(app.point[1] - 100) <= 2, app.point)
                    self.assertTrue(app.canvas.find_withtag("marker"))
                    self.assertTrue(app.point_bar.winfo_ismapped())
                    app.chat_input.insert("1.0", "What is this?")
                    app.send_or_stop()
                    self.assertIsNone(app.point)
                    deadline = time.monotonic() + 10
                    while app.runs and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.02)
                    self.assertTrue(store.messages(app.thread["id"])[-3]["text"].startswith("◉ ("))
                    first, second, third = [json.loads(line) for line in calls.read_text().splitlines()]
                    # Holding the right button and dragging frames a region, in whatever direction it is dragged.
                    app.canvas.event_generate("<Button-3>", x=width // 2 + 50, y=height // 2 + 20)
                    app.canvas.event_generate("<B3-Motion>", x=width // 2, y=height // 2)
                    root.update()
                    self.assertEqual(app.canvas.type(app.canvas.find_withtag("marker")[0]), "rectangle")  # drawn while dragging
                    app.canvas.event_generate("<B3-Motion>", x=width // 2 - 40, y=height // 2 - 30)
                    app.canvas.event_generate("<ButtonRelease-3>", x=width // 2 - 40, y=height // 2 - 30)
                    root.update()
                    left, top, right, bottom = app.point
                    self.assertTrue(all(abs(found - wanted) <= 2 for found, wanted in zip(app.point, (110, 70, 200, 120))), app.point)
                    self.assertIn(f"{right - left} × {bottom - top} px", app.point_label.cget("text"))
                    self.assertEqual(root.focus_get(), app.chat_input)
                    app.chat_input.insert("1.0", "And this part?")
                    app.send_or_stop()
                    self.assertIsNone(app.point)
                    self.assertFalse(app.canvas.find_withtag("marker"))
                    deadline = time.monotonic() + 10
                    while app.runs and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.02)
                    self.assertTrue(store.messages(app.thread["id"])[-3]["text"].startswith(f"▭ ({left}, {top})–({right}, {bottom})"))
                    fourth = json.loads(calls.read_text().splitlines()[3])
                    self.assertIn(f"from x={left}, y={top} to x={right}, y={bottom}", fourth[-1])
                    self.assertTrue(all(Path(path).exists() for path in re.findall(r"\((/[^();]+\.png)\)", fourth[-1])))
                    self.assertIn("y=", third[-1])
                    marks = re.findall(r"\((/[^();]+\.png)\)", third[-1])
                    self.assertEqual(len(marks), 2)
                    self.assertTrue(all(Path(path).exists() for path in marks))
                    self.assertIn("--add-dir", third)
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

    def test_review_fixes_layout_focus_versions_and_empty_states(self):
        import tkinter as tk
        from agent_visor.app import Visor, CHAT_MIN_WINDOW
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            directory = Path(directory)
            store = Store(directory / "data")
            root = tk.Tk()
            try:
                store.set_setting("geometry", "820x540")
                app = Visor(root, store)
                root.update()
                self.assertIn("No projects yet", app.session_button.cget("text"))
                self.assertEqual(str(app.session_button.cget("state")), "disabled")
                one, two = directory / "one.png", directory / "two.png"
                Image.new("RGB", (300, 200), "white").save(one)
                Image.new("RGB", (300, 200), "black").save(two)
                items = store.ingest([str(one)], "Fix") + store.ingest([str(two)], "Fix")
                app.dispatch({"action": "received", "items": items[-1:]})
                root.update()
                def labels(widget):
                    for child in widget.winfo_children():
                        if isinstance(child, tk.Label):
                            yield child.cget("text")
                        yield from labels(child)
                tags = [text for card in app.cards.values() for text in labels(card) if text.isupper()]
                self.assertEqual(tags, ["LATEST"])  # two different files: no version tags
                self.assertEqual(app.count_label.cget("text"), "2 / 2")
                self.assertIn("two.png   ·   " + str(directory), app.detail_label.cget("text"))
                # The panel without a session explains itself and offers nothing to click.
                app.toggle_chat()
                root.update()
                self.assertEqual(root.minsize()[0], CHAT_MIN_WINDOW)
                self.assertGreaterEqual(root.winfo_width(), CHAT_MIN_WINDOW)
                self.assertGreaterEqual(app.canvas.winfo_width() / root.winfo_width(), .4)
                self.assertEqual(app.chat_bottom.winfo_manager(), "")
                self.assertEqual(app.chat_actions.winfo_manager(), "")
                self.assertEqual(app.ask_button.kind, "chosen")
                app.toggle_chat()
                self.assertEqual(root.minsize()[0], 820)
                self.assertEqual(app.ask_button.kind, "plain")
                # Closing the sidebar gives its width to the figure, and the choice is remembered.
                root.update()
                before = app.canvas.winfo_width()
                app.toggle_sidebar()
                root.update()
                self.assertFalse(app.sidebar.winfo_ismapped())
                self.assertEqual(app.canvas.winfo_width(), before + app.sidebar.winfo_reqwidth())
                self.assertFalse(Store(directory / "data").setting("sidebar_open"))
                app.set_language("es")
                root.update()
                self.assertFalse(app.sidebar.winfo_ismapped())
                app.set_language("en")
                app.toggle_sidebar()
                root.update()
                self.assertTrue(app.sidebar.winfo_ismapped())
                self.assertLess(app.sidebar.winfo_x(), app.main.winfo_x())
                # A theme repaints the whole window, keeps what is shown and is remembered.
                shown = app.current["id"]
                for theme, background in (("dark", "#262624"), ("sky", "#edf1f6"), ("stone", "#f1f1f0"), ("sand", "#f0eee6")):
                    app.set_theme(theme)
                    root.update()
                    self.assertEqual(app.main.cget("bg"), background)
                    self.assertEqual(app.canvas.master.cget("bg"), root.cget("bg"))
                    self.assertEqual(app.current["id"], shown)
                    self.assertEqual(Store(directory / "data").setting("theme"), theme)
                # Every button shows a focus ring.
                def buttons(widget):
                    for child in widget.winfo_children():
                        if isinstance(child, (tk.Button, tk.Checkbutton)):
                            yield child
                        yield from buttons(child)
                self.assertTrue(all(int(b.cget("highlightthickness")) >= 2 for b in buttons(root)))
                # A filter that hides everything also empties the canvas.
                with store.connect() as conn:
                    conn.execute("UPDATE images SET created = created - 7200")
                app.set_period("hour")
                root.update()
                self.assertIsNone(app.current)
                self.assertEqual(app.title_label.cget("text"), "Nothing in this period")
                self.assertEqual(app.zoom_bar.winfo_manager(), "")  # no zoom without an image
                self.assertTrue(app.show_all_button.winfo_ismapped())
                app.show_all_button.invoke()
                root.update()
                self.assertEqual(app.period, "all")
                self.assertEqual(app.current["id"], items[-1]["id"])
                self.assertEqual(app.zoom_bar.winfo_manager(), "pack")
                # At the smallest window the detail line stays inside the window.
                root.geometry("820x540")
                root.update()
                self.assertLessEqual(app.detail_label.winfo_y() + app.detail_label.winfo_height(), app.main.winfo_height())
                # Same with the filter already on when the project is (re)selected.
                app.period = "hour"
                app.select_session("Fix")
                self.assertIsNone(app.current)
                app.set_period("all")
                # The panel's explanation wraps inside the panel at its narrowest width.
                app.toggle_chat()
                root.update()
                self.assertLessEqual(int(app.chat_status.cget("wraplength")), int(app.chat.cget("width")) - 36)
                app.toggle_chat()
            finally:
                root.destroy()

    def test_conversation_browser_groups_filters_and_opens(self):
        import tkinter as tk
        from agent_visor.app import Visor
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            directory = Path(directory)
            image = directory / "plot.png"
            Image.new("RGB", (300, 200), "white").save(image)
            store = Store(directory / "data")
            for session in ("Atlas / landing", "Atlas / static", "Notes"):
                store.ingest([str(image)], session)
            root = tk.Tk()
            try:
                app = Visor(root, store)
                root.update()
                self.assertEqual(app.session, "Notes")
                self.assertIn("Notes", app.session_button.cget("text"))
                app.session_button.invoke()
                root.update()
                self.assertTrue(app.browser_view.winfo_ismapped())
                self.assertFalse(app.history_view.winfo_ismapped())
                # Newest first; the two conversations of Atlas sit together under its name.
                self.assertEqual([session for session, _ in app.browser_rows], ["Notes", "Atlas / static", "Atlas / landing"])
                texts = [child.cget("text") for child in app.browser_frame.winfo_children() if isinstance(child, tk.Label)]
                self.assertEqual(texts, ["Atlas"])
                self.assertEqual(app.browser_index, 0)  # the keyboard starts on the conversation shown
                # Typing in the filter must not trigger the window shortcuts.
                app.filter_entry.focus_force()
                root.update()
                app.key(lambda: self.fail("shortcut fired while filtering"))(None)
                app.filter_var.set("atlas land")
                self.assertEqual([session for session, _ in app.browser_rows], ["Atlas / landing"])
                app.filter_var.set("nothing like this")
                self.assertEqual(app.browser_rows, [])
                app.filter_var.set("atlas")
                app.move_browser(1)
                app.pick_browser()
                root.update()
                self.assertEqual(app.session, "Atlas / landing")
                self.assertTrue(app.history_view.winfo_ismapped())
                self.assertIn("Atlas", app.session_button.cget("text"))
                self.assertEqual(app.session_detail.cget("text"), "landing")
                self.assertEqual(app.current["session"], "Atlas / landing")
                # A new image arriving while browsing refreshes the list in place.
                app.toggle_browser()
                app.follow.set(False)
                app.dispatch({"action": "received", "items": store.ingest([str(image)], "Fresh")})
                self.assertEqual(app.browser_rows[0][0], "Fresh")
                app.set_language("es")
                root.update()
                self.assertTrue(app.history_view.winfo_ismapped())
            finally:
                root.destroy()

    def test_second_review_tab_order_running_stop_and_queued_position(self):
        import tkinter as tk
        from agent_visor.app import Visor
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            directory = Path(directory)
            slow = directory / "claude"
            slow.write_text(f"#!{sys.executable}\nimport json, time\n"
                            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'fork-1'}), flush=True)\n"
                            "time.sleep(60)\n")
            slow.chmod(0o755)
            image = directory / "plot.png"
            Image.new("RGB", (300, 200), "white").save(image)
            store = Store(directory / "data")
            first = store.ingest([str(image)], "Q", agent_session="31cde43d-3856-47e7-990f-9d87748f1e6d", agent_cwd=str(directory))[0]
            root = tk.Tk()
            try:
                with patch("agent_visor.chat.claude_binary", return_value=str(slow)):
                    app = Visor(root, store)
                    root.update()
                    # Tab follows the visual order of the zoom controls, left to right.
                    minus = app.zoom_bar.winfo_children()[0]
                    order = [minus.cget("text")]
                    widget = minus
                    for _ in range(3):
                        widget = widget.tk_focusNext()
                        order.append(widget.cget("text"))
                    self.assertEqual(order, ["−", "+", "1:1", "Fit"])
                    # While a question runs the panel says so; stopping is a note, not an error.
                    app.toggle_chat()
                    app.chat_input.insert("1.0", "Why?")
                    app.send_or_stop()
                    root.update()
                    self.assertTrue(app.working_label.winfo_ismapped())
                    self.assertEqual(app.send_button.cget("text"), "Stop")
                    app.send_or_stop()
                    deadline = time.monotonic() + 10
                    while app.runs and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.02)
                    root.update()
                    self.assertEqual(store.messages(app.thread["id"])[-1], {"role": "note", "text": "Stopped."})
                    self.assertFalse(app.working_label.winfo_ismapped())
                    self.assertEqual(app.send_button.cget("text"), "Send")
                    # With Follow latest off, a queued image keeps the position "k / n" of the one shown.
                    app.follow.set(False)
                    newer = store.ingest([str(image)], "Q")
                    self.assertTrue(app.dispatch({"action": "received", "items": newer})["queued"])
                    root.update()
                    self.assertEqual(app.current["id"], first["id"])
                    self.assertEqual(app.count_label.cget("text"), "1 / 2")
            finally:
                root.destroy()

    def test_bottom_row_fits_beside_the_panel_in_both_languages(self):
        import tkinter as tk
        from agent_visor.app import Visor
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            directory = Path(directory)
            pdf = directory / "talk.pdf"
            pages = [Image.new("RGB", (160, 90), "white") for _ in range(12)]
            pages[0].save(pdf, save_all=True, append_images=pages[1:])
            store = Store(directory / "data")
            store.ingest([str(pdf)], "Fit", agent_session="31cde43d-3856-47e7-990f-9d87748f1e6d")
            store.set_setting("geometry", "820x540")
            store.set_setting("chat_open", True)
            root = tk.Tk()
            try:
                app = Visor(root, store)
                for language in ("en", "es"):
                    app.set_language(language)
                    for _ in range(9):
                        app.turn(1)  # "Slide 10 / 12" is the widest label
                    settle(app)
                    row = app.zoom_bar.master
                    for bar in (app.slide_bar, app.zoom_bar):
                        for child in bar.winfo_children():
                            right = bar.winfo_x() + child.winfo_x() + child.winfo_width()
                            self.assertLessEqual(right, row.winfo_width(), f"{language}: {child.cget('text')!r} is cut off")
                    self.assertLess(app.slide_bar.winfo_x() + app.slide_bar.winfo_width(), app.zoom_bar.winfo_x())
            finally:
                root.destroy()

    def test_every_control_has_a_visible_focus_ring(self):
        """WCAG non-text contrast: the focus ring differs at least 3:1 from what surrounds it, on every control."""
        import tkinter as tk
        from agent_visor.app import Visor

        def luminance(color, widget):
            red, green, blue = (value / 65535 for value in widget.winfo_rgb(color))
            linear = [c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4 for c in (red, green, blue)]
            return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]

        def ratio(first, second, widget):
            high, low = sorted((luminance(first, widget), luminance(second, widget)), reverse=True)
            return (high + .05) / (low + .05)

        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            directory = Path(directory)
            pdf = directory / "talk.pdf"
            pages = [Image.new("RGB", (160, 90), "white") for _ in range(2)]
            pages[0].save(pdf, save_all=True, append_images=pages[1:])
            store = Store(directory / "data")
            store.ingest([str(pdf)], "Ring", agent_session="31cde43d-3856-47e7-990f-9d87748f1e6d")
            store.set_setting("chat_open", True)
            root = tk.Tk()
            try:
                app = Visor(root, store)
                app.point = (10, 10)
                app.show_point()
                app.show_all_button.place(x=0, y=0)  # normally only shown on an empty filter
                root.update()
                checked = 0
                stack = [root]
                while stack:
                    widget = stack.pop()
                    stack.extend(widget.winfo_children())
                    if isinstance(widget, (tk.Button, tk.Checkbutton)):
                        name = widget.cget("text")
                        self.assertGreaterEqual(int(widget.cget("highlightthickness")), 1, name)
                        around = widget.master.cget("bg")
                        self.assertGreaterEqual(ratio(widget.cget("highlightcolor"), around, widget), 3, name)
                        self.assertEqual(widget.winfo_rgb(widget.cget("highlightbackground")), widget.winfo_rgb(around), name)
                        checked += 1
                self.assertGreater(checked, 20)
                # The question box shows focus on its own rounded edge, which is invisible otherwise.
                edge, around = app.composer_ring, app.composer_ring.master.cget("bg")
                self.assertEqual(edge.cget("bg"), app.chat_input.cget("bg"))
                app.chat_input.focus_force()
                root.update()
                self.assertGreaterEqual(ratio(edge.cget("bg"), around, edge), 3)
                self.assertGreaterEqual(ratio(edge.cget("bg"), app.chat_input.cget("bg"), edge), 3)
                app.canvas.focus_force()
                root.update()
                self.assertEqual(edge.cget("bg"), app.chat_input.cget("bg"))
            finally:
                root.destroy()

    def test_hidpi_scales_fixed_sizes(self):
        import tkinter as tk
        from agent_visor.app import Visor
        with tempfile.TemporaryDirectory(prefix="av-ui-") as directory:
            directory = Path(directory)
            pdf = directory / "talk.pdf"
            pages = [Image.new("RGB", (160, 90), "white") for _ in range(3)]
            pages[0].save(pdf, save_all=True, append_images=pages[1:])
            store = Store(directory / "data")
            store.ingest([str(pdf)], "HiDPI")
            root = tk.Tk()
            original = root.tk.call("tk", "scaling")  # Tk keeps scaling per display, shared by later tests
            try:
                root.tk.call("tk", "scaling", 2 * 96 / 72)  # a 192 dpi screen
                app = Visor(root, store)
                root.geometry("1640x1000")
                root.update()
                self.assertEqual(app.factor, 2)
                self.assertEqual(app.sidebar.winfo_width(), 544)
                # No label or button gets less room than it asks for (Tk shrinks a widget rather than overflow,
                # so a clipped text shows up as width < reqwidth). The detail line truncates long paths on purpose.
                clipped, stack = [], [root]
                while stack:
                    widget = stack.pop()
                    stack.extend(widget.winfo_children())
                    if (isinstance(widget, (tk.Label, tk.Button, tk.Checkbutton)) and widget.winfo_ismapped()
                            and widget is not app.detail_label
                            and (widget.winfo_width() < widget.winfo_reqwidth() or widget.winfo_height() < widget.winfo_reqheight())):
                        clipped.append(widget.cget("text"))
                self.assertEqual(clipped, [])
            finally:
                root.tk.call("tk", "scaling", original)
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

    @unittest.skipUnless(graphical(), "No graphical desktop access")
    def test_shortcut_names_the_class_tk_gives_the_window(self):
        # GNOME puts the shortcut's icon on the window only when StartupWMClass is the window's class, letter for letter.
        import tkinter as tk
        name = re.search(r'tk\.Tk\(className="(\w+)"\)', (ROOT / "agent_visor/app.py").read_text()).group(1)
        root = tk.Tk(className=name)
        window_class = root.winfo_class()
        root.destroy()
        spec = importlib.util.spec_from_file_location("visor_install", ROOT / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix="av-install-") as directory:
            module.install(Path(directory), sys.executable)
            shortcut = (Path(directory) / ".local/share/applications/agent-visor.desktop").read_text()
        self.assertIn(f"StartupWMClass={window_class}\n", shortcut)


if __name__ == "__main__":
    unittest.main()
