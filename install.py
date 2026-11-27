#!/usr/bin/env python3
"""Install only the user-level command, skill links and desktop shortcut."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
MARKER = "# agent-visor launcher"


def install(home, interpreter, desktop=True):
    subprocess.run([interpreter, "-c", "import tkinter; from PIL import Image"], check=True)
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
            f"Exec={quoted(str(command))} open\nIcon={ROOT / 'assets/visor.svg'}\nTerminal=false\nCategories=Graphics;Utility;\nStartupWMClass=Agentvisor\n")
    print(f"Command installed: {command}")
    print("Skill installed for Codex and Claude Code (available in new sessions).")
    print(f"Try: {command} demo")


def bitmap_tk(prefix):
    """The version of the Tk in a conda environment when it is the build without Xft, else None. That build only
    has the old X bitmap fonts: the window's text comes out in a fallback face and its symbols have no glyph."""
    for meta in Path(prefix).glob("conda-meta/tk-*.json"):
        package = json.loads(meta.read_text())
        if package["name"] == "tk" and package["build"].startswith("noxft"):
            return package["version"]


def fix_tk(interpreter, fix):
    """Offer to replace a Tk without Xft with the build that has it; with fix, replace it without asking."""
    prefix = subprocess.run([interpreter, "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True, check=True).stdout.strip()
    version = bitmap_tk(prefix)
    if not version:
        return
    print(f"The Tk of {prefix} is built without Xft: Visor will show bitmap fonts and miss its symbols.")
    tools = [os.environ.get("MAMBA_EXE"), os.environ.get("CONDA_EXE"), *map(shutil.which, ("micromamba", "mamba", "conda"))]
    command = [next(filter(None, tools), "conda"), "install", "-y", "-p", prefix, "-c", "conda-forge", f"tk={version}=xft_*"]
    if not fix and sys.stdin.isatty():
        fix = input("Replace it with the Xft build from conda-forge (about 4 MB)? [Y/n] ").strip().lower() in ("", "y", "yes")
    if not fix:
        print(f"To fix it later: {shlex.join(command)}")
        return
    subprocess.run(command, check=True)
    print("Tk replaced. Run `visor stop` if Visor is open; it uses the new Tk the next time it opens.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--no-desktop", action="store_true")
    parser.add_argument("--fix-tk", action="store_true", help="replace a conda Tk built without Xft without asking")
    args = parser.parse_args()
    try:
        interpreter = str(Path(args.python).expanduser().absolute())
        install(args.home, interpreter, not args.no_desktop)
        fix_tk(interpreter, args.fix_tk)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Install: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
