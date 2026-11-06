#!/usr/bin/env python3
"""Install only the user-level command, skill links and desktop shortcut."""
import argparse
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
MARKER = "# agent-visor launcher"


def install(home, interpreter, desktop=True):
    subprocess.run([interpreter, "-c", "import tkinter; from PIL import Image, ImageTk"], check=True)
    command = home / ".local/bin/visor"
    skill = ROOT / "skills/agent-visor"
    links = [home / ".codex/skills/agent-visor", home / ".claude/skills/agent-visor"]
    if command.exists() and (command.is_symlink() or MARKER not in command.read_text()):
        raise RuntimeError(f"Ya existe otro comando en {command}; no se ha reemplazado.")
    for link in links:
        if (link.exists() or link.is_symlink()) and not (link.is_symlink() and link.resolve() == skill):
            raise RuntimeError(f"Ya existe otra habilidad en {link}; no se ha reemplazado.")
    command.parent.mkdir(parents=True, exist_ok=True)
    command.write_text(f"#!/bin/sh\n{MARKER}\nexec {shlex.quote(interpreter)} {shlex.quote(str(ROOT / 'launch.py'))} \"$@\"\n")
    command.chmod(0o755)
    for link in links:
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink():
            link.symlink_to(skill, target_is_directory=True)
    if desktop:
        shortcut = home / ".local/share/applications/agent-visor.desktop"
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        def quoted(value):
            for char in ('\\', '"', '`', '$'):
                value = value.replace(char, '\\' + char)
            return '"' + value + '"'
        shortcut.write_text("[Desktop Entry]\nType=Application\nName=Visor\nComment=Tu espacio visual para agentes de terminal\n"
            f"Exec={quoted(str(command))} open\nIcon={ROOT / 'assets/visor.svg'}\nTerminal=false\nCategories=Graphics;Utility;\nStartupWMClass=AgentVisor\n")
    print(f"Comando instalado: {command}")
    print("Habilidad instalada para Codex y Claude Code (disponible en sesiones nuevas).")
    print(f"Prueba: {command} demo")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--no-desktop", action="store_true")
    args = parser.parse_args()
    try:
        install(args.home, str(Path(args.python).expanduser().absolute()), not args.no_desktop)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Instalación: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
