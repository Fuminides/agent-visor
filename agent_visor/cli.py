from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .ipc import ensure_running, request, running
from .storage import Store, data_dir, load_image


def default_session():
    return os.environ.get("VISOR_SESSION") or str(Path.cwd())


def parser():
    top = argparse.ArgumentParser(prog="visor", description="Tu ventana de imágenes para agentes de terminal. Se abre sola cuando hace falta.")
    top.add_argument("--version", action="version", version=f"visor {__version__}")
    commands = top.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="Muestra imágenes; abre o reutiliza la ventana automáticamente")
    show.add_argument("paths", nargs="+", type=Path)
    show.add_argument("--title", default="", help="Título de las imágenes")
    show.add_argument("--caption", default="", help="Descripción o conclusión breve")
    show.add_argument("--session", default=None, help="Proyecto o conversación (por defecto: carpeta actual)")
    show.add_argument("--focus", action="store_true", help="Trae la ventana al frente expresamente")
    show.add_argument("--json", action="store_true")
    op = commands.add_parser("open", help="Abre el visor y restaura el historial")
    op.add_argument("--session")
    op.add_argument("--json", action="store_true")
    for name, helptext in [("status", "Comprueba el estado sin abrir la ventana"), ("stop", "Cierra el visor conservando el historial")]:
        sub = commands.add_parser(name, help=helptext)
        sub.add_argument("--json", action="store_true")
    history = commands.add_parser("history", help="Consulta el historial sin abrir la ventana")
    history.add_argument("--session")
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--json", action="store_true")
    demo = commands.add_parser("demo", help="Abre dos gráficos de ejemplo")
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
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            state = running() or {"ok": True, "running": False}
            emit(state, args.json, "Visor abierto." if state.get("running") else "Visor cerrado. Se abrirá al mostrar una imagen.")
            return 0
        if args.command == "stop":
            state = request({"action": "stop"}) if running() else {"ok": True, "running": False}
            emit(state, args.json, "Visor cerrado. Historial conservado.")
            return 0
        if args.command == "history":
            if args.limit < 1 or args.limit > 1000:
                raise ValueError("--limit debe estar entre 1 y 1000.")
            directory = data_dir()
            items = Store().history(args.session, args.limit) if (directory / "history.sqlite3").exists() else []
            emit({"ok": True, "items": items}, args.json,
                 "\n".join(f"{item['session']} · {item['title']} · {item['source']}" for item in items) or "Aún no hay imágenes.")
            return 0
        if args.command == "demo":
            from .demo import make_demo
            paths = make_demo()
            ensure_running()
            result = request({"action": "show", "paths": list(map(str, paths)), "session": "Demo · Visor", "caption": "Un ejemplo de lo que puede mostrarte tu agente."})
        elif args.command == "open":
            ensure_running()
            result = request({"action": "open", "session": args.session, "focus": True})
        else:
            paths = [path.expanduser().resolve(strict=True) for path in args.paths]
            if len(paths) > 30:
                raise ValueError("Envía como máximo 30 imágenes por llamada.")
            # Invalid inputs must not spawn an empty window or add partial history.
            for path in paths:
                image = load_image(path)
                image.close()
            session = args.session or default_session()
            if not session.strip() or len(session) > 200:
                raise ValueError("El proyecto debe tener entre 1 y 200 caracteres. Usa --session.")
            ensure_running()
            result = request({"action": "show", "paths": list(map(str, paths)), "session": session,
                "title": args.title, "caption": args.caption, "focus": args.focus})
        verb = "Añadido al historial" if result.get("queued") else "Mostrado en Visor"
        emit(result, args.json, f"{verb} · {result.get('session') or 'listo'}")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"visor: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
