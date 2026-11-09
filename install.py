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
        raise RuntimeError(f"Another command already exists at {command}; it was not replaced.")
    for link in links:
        if (link.exists() or link.is_symlink()) and not (link.is_symlink() and link.resolve() == skill):
            raise RuntimeError(f"Another skill already exists at {link}; it was not replaced.")
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
        shortcut.write_text("[Desktop Entry]\nType=Application\nName=Visor\nComment=Your visual workspace for terminal agents\n"
            f"Exec={quoted(str(command))} open\nIcon={ROOT / 'assets/visor.svg'}\nTerminal=false\nCategories=Graphics;Utility;\nStartupWMClass=AgentVisor\n")
    print(f"Command installed: {command}")
    print("Skill installed for Codex and Claude Code (available in new sessions).")
    print(f"Try: {command} demo")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--no-desktop", action="store_true")
    args = parser.parse_args()
    try:
        install(args.home, str(Path(args.python).expanduser().absolute()), not args.no_desktop)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Install: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
