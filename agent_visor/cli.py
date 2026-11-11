from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from . import __version__, mirror
from .chat import origin
from .ipc import ensure_running, request, running
from .runs import REMOTE, fetch
from .storage import MAX_PAGES, Store, data_dir, page_count


def default_session():
    return os.environ.get("VISOR_SESSION") or str(Path.cwd())


def parser():
    top = argparse.ArgumentParser(prog="visor", description="An image window for terminal coding agents. It opens by itself when needed.")
    top.add_argument("--version", action="version", version=f"visor {__version__}")
    commands = top.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="Show images or PDFs; opens or reuses the window automatically")
    show.add_argument("paths", nargs="+", type=Path)
    show.add_argument("--title", default="", help="Title of the images")
    show.add_argument("--caption", default="", help="Short description or takeaway")
    show.add_argument("--session", default=None, help="Project or conversation (default: current folder)")
    show.add_argument("--deck", action="store_true", help="Join every image of the call into one presentation")
    show.add_argument("--focus", action="store_true", help="Bring the window to the front")
    show.add_argument("--json", action="store_true")
    track = commands.add_parser("track", help="Follow the progress file of a training or evaluation as a live entry")
    track.add_argument("source", help="Log or JSON-lines file the run appends to; host:/path reads it over ssh")
    track.add_argument("--title", default="", help="Title of the run")
    track.add_argument("--note", default="", help="A remark shown with the run; every call adds one")
    track.add_argument("--metrics", default=None, help="Comma-separated keys to draw as curves (default: every number)")
    track.add_argument("--session", default=None, help="Project or conversation (default: current folder)")
    track.add_argument("--focus", action="store_true", help="Bring the window to the front")
    track.add_argument("--json", action="store_true")
    op = commands.add_parser("open", help="Open Visor and restore the history")
    op.add_argument("--session")
    op.add_argument("--json", action="store_true")
    for name, helptext in [("status", "Check the state without opening the window"), ("stop", "Close Visor, keeping the history")]:
        sub = commands.add_parser(name, help=helptext)
        sub.add_argument("--json", action="store_true")
    follow = commands.add_parser("follow", help="Show here what agents show on another computer, fetched over ssh")
    follow.add_argument("host", help="The other computer: a host name from your ssh config, with Visor installed there")
    follow.add_argument("--off", action="store_true", help="Stop following that computer")
    follow.add_argument("--json", action="store_true")
    feed = commands.add_parser("feed", help="Print new history entries as JSON lines; `visor follow` on another computer reads them")
    feed.add_argument("--images", type=int, help="Continue after this entry")
    feed.add_argument("--runs", type=int, help="Continue after this run")
    feed.add_argument("--once", action="store_true", help="Print what there is and end")
    history = commands.add_parser("history", help="List the history without opening the window")
    history.add_argument("--session")
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--json", action="store_true")
    demo = commands.add_parser("demo", help="Show two example plots")
    demo.add_argument("--json", action="store_true")
    return top


def emit(result, as_json, message):
    print(json.dumps(result, ensure_ascii=False) if as_json else message)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["_serve"]:
        from .app import run
        run()
        return 0
    if argv[:1] == ["_askpass"]:
        from .askpass import main as ask
        return ask(" ".join(argv[1:]))
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            state = running() or {"ok": True, "running": False}
            emit(state, args.json, "Visor is open." if state.get("running") else "Visor is closed. It opens when an image is shown.")
            return 0
        if args.command == "stop":
            state = request({"action": "stop"}) if running() else {"ok": True, "running": False}
            emit(state, args.json, "Visor closed. History kept.")
            return 0
        if args.command == "history":
            if args.limit < 1 or args.limit > 1000:
                raise ValueError("--limit debe estar entre 1 y 1000.")
            directory = data_dir()
            items = Store().history(args.session, args.limit) if (directory / "history.sqlite3").exists() else []
            emit({"ok": True, "items": items}, args.json,
                 "\n".join(f"{item['session']} · {item['title']} · {item['source']}" for item in items) or "No images yet.")
            return 0
        if args.command == "feed":
            try:
                mirror.feed(Store(), {"images": args.images, "runs": args.runs}, sys.stdout, follow=not args.once)
            except BrokenPipeError:  # the follower left
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            return 0
        if args.command == "follow":
            if not mirror.HOST.fullmatch(args.host):
                raise ValueError("Give the other computer by a host name from your ssh config.")
            ensure_running()
            result = request({"action": "follow", "host": args.host, "off": args.off})
            if args.off:
                emit(result, args.json, f"No longer following {args.host}.")
                return 0
            # The window connects, asking for a password in a dialog if ssh needs one; say how it went.
            patience = time.monotonic() + 120
            while result["mirrors"].get(args.host) == "connecting" and time.monotonic() < patience:
                time.sleep(.5)
                result = request({"action": "status"})
            state = result["mirrors"].get(args.host)
            if state == "connecting":
                emit(result, args.json, f"Still connecting to {args.host}: answer the dialog Visor opened. `visor status --json` says how it went.")
                return 0
            if state != "connected":
                raise RuntimeError(f"Cannot follow {args.host}: {state}")
            emit(result, args.json, f"Following {args.host}: what agents show there appears here.")
            return 0
        if args.command == "demo":
            from .demo import make_demo
            paths = make_demo()
            ensure_running()
            result = request({"action": "show", "paths": list(map(str, paths)), "session": "Demo · Visor", "caption": "An example of what your agent can show you."})
        elif args.command == "open":
            ensure_running()
            result = request({"action": "open", "session": args.session, "focus": True})
        else:
            # Invalid inputs must not spawn an empty window or add partial history.
            if args.command == "track":
                source = args.source if REMOTE.match(args.source) else str(Path(args.source).expanduser().resolve())
                fetch(source, 0, 0)
                message = {"action": "track", "source": source, "title": args.title, "note": args.note,
                           "metrics": None if args.metrics is None else [key.strip() for key in args.metrics.split(",") if key.strip()]}
            else:
                paths = [path.expanduser().resolve(strict=True) for path in args.paths]
                if len(paths) > 30:
                    raise ValueError("Send at most 30 images per call.")
                if sum(page_count(path) for path in paths) > MAX_PAGES:
                    raise ValueError(f"Send at most {MAX_PAGES} images or pages per call.")
                message = {"action": "show", "paths": list(map(str, paths)), "title": args.title, "caption": args.caption,
                           "deck": args.deck}
            session = args.session or default_session()
            if not session.strip() or len(session) > 200:
                raise ValueError("The project name must have 1 to 200 characters. Use --session.")
            ensure_running()
            # Rendering a long PDF can take a while; the window answers once every page is stored.
            result = request({**message, "session": session, "focus": args.focus,
                              **origin()}, timeout=300)
        verb = "Added to history" if result.get("queued") else "Shown in Visor"
        if args.command == "track":
            # What was understood so far, so a log in a format Visor cannot read is noticed at once.
            if result.get("error"):
                read = f"cannot read the file: {result['error']}"
            elif result["step"] is None and not result["metrics"]:
                read = 'no progress line yet (lines like "[3/12] acc=0.91" or {"step": 3, "total": 12, "acc": 0.91})'
            else:
                total = f"/{result['total']:g}" if result["total"] else ""
                read = "  ·  ".join(filter(None, [f"step {result['step']:g}{total}" if result["step"] is not None else "",
                                                 "curves: " + ", ".join(result["metrics"]) if result["metrics"] else ""]))
            verb = f"Following · {read}"
        emit(result, args.json, f"{verb} · {result.get('session') or 'ready'}")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"visor: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
