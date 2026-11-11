"""Origin metadata and prompts for questions about a figure. No GUI imports here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import uuid

from PIL import Image, ImageDraw

SESSION_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
# The fork can look at the project but never change it: no Bash, no edits, no MCP servers.
READ_ONLY_TOOLS = "Read,Grep,Glob"
RED = (229, 72, 77)


def origin():
    """Capture the caller, never the environment of the long-running Visor window."""
    claude = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    codex = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID", "")
    session, provider = (claude, "claude") if SESSION_ID.fullmatch(claude) else (codex, "codex")
    if not SESSION_ID.fullmatch(session):
        return {}
    from .codex import control_socket
    return dict(agent_session=session, agent_provider=provider, agent_cwd=str(Path.cwd()),
                agent_socket=control_socket() if provider == "codex" else None)


def claude_binary():
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local/bin/claude"
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("The claude command was not found.")


def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def session_cwd(session_id, fallback=None):
    """Folder the session was started in, read from its transcript; else where `visor show` ran."""
    for transcript in projects_dir().glob(f"*/{session_id}.jsonl"):
        with transcript.open(encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(200), handle):
                try:
                    cwd = json.loads(line).get("cwd")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if cwd and Path(cwd).is_dir():
                    return cwd
    return fallback if fallback and Path(fallback).is_dir() else str(Path.home())


# Set by the agent session that happened to start Visor; they would tie the fork to that session.
SESSION_VARIABLES = {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_MESSAGING_SOCKET",
                     "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ATTENDED",
                     "CLAUDE_CODE_EXECPATH", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT"}


def clean_env():
    return {key: value for key, value in os.environ.items() if key not in SESSION_VARIABLES}


def command(session_id, fork_id, prompt, readable=()):
    """First question forks the original session; later ones continue that fork.
    readable: folders outside the project the fork may read (Visor's snapshots and marked points)."""
    args = [claude_binary(), "-p", "--resume", fork_id or session_id]
    if not fork_id:
        args.append("--fork-session")
    args += ["--tools", READ_ONLY_TOOLS, "--strict-mcp-config", "--permission-mode", "dontAsk"]
    for folder in readable:
        args += ["--add-dir", str(folder)]
    # The prompt goes last, after a single-value option, so no list option swallows it.
    return args + ["--output-format", "stream-json", "--verbose", prompt]


def ring(draw, x, y, radius, width):
    """Red circle with a white halo, visible on light and dark figures."""
    box = (x - radius, y - radius, x + radius, y + radius)
    draw.ellipse(box, outline="white", width=width + 3)
    draw.ellipse(box, outline=RED, width=width)


def mark_point(snapshot, x, y, directory):
    """Save the figure with the point circled and a zoom around it, for the fork to look at."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with Image.open(snapshot) as source:
        image = source.convert("RGB")
    side = max(image.size)
    marked = image.copy()
    ring(ImageDraw.Draw(marked), x, y, max(10, round(side * .018)), max(3, round(side * .004)))
    half = max(120, round(side * .125))
    box = (max(0, x - half), max(0, y - half), min(image.width, x + half), min(image.height, y + half))
    factor = max(1, 768 / max(box[2] - box[0], box[3] - box[1]))
    zoom = image.crop(box)
    zoom = zoom.resize((round(zoom.width * factor), round(zoom.height * factor)), Image.Resampling.LANCZOS)
    ring(ImageDraw.Draw(zoom), (x - box[0]) * factor, (y - box[1]) * factor, 22, 4)
    name = uuid.uuid4().hex
    paths = directory / f"{name}.png", directory / f"{name}.zoom.png"
    marked.save(paths[0])
    zoom.save(paths[1])
    image.close()
    return tuple(map(str, paths))


def prompt(question, item, first, point=None, live=False):
    if item.get("kind") == "run":  # a live run: the fork reads what Visor has gathered, since the file may be on another machine
        where = (f"the live run \"{item['title']}\" (its progress file is {item['source']}; the progress, curves and "
                 f"notes Visor has read from it are saved at {item['dump']})")
        shown = f"{where}, which you followed with `visor track`"
    else:
        where = f"\"{item['title']}\" ({item['source']}; saved copy at {item['snapshot']})"
        if item.get("deck"):
            where += f", slide {item['page']} of {item['pages']}"
        shown = f"the figure {where}, which you showed with `visor show`"
    if point:
        question = (f"[The user pointed at the figure: x={point['x']}, y={point['y']} px of "
                    f"{item['width']}×{item['height']}, origin at the top left ({100 * point['x'] / item['width']:.0f} % "
                    f"of the width, {100 * point['y'] / item['height']:.0f} % of the height). They are asking about the "
                    f"element at or near that point. Use Read to look at the figure with the point circled in red "
                    f"({point['marked']}) and at the zoom around the point ({point['zoom']}).]\n\n{question}")
    if item.get("agent_provider") == "codex":
        question = question.replace("Use Read to look at", "Use your image-viewing tool to look at")
    if live:
        return (f"[Message sent from Visor to the original session] The user is looking at {shown}. "
                "Answer in the user's language, briefly and with little Markdown because Visor shows plain text. "
                "This message belongs to the existing conversation; keep its ongoing work and settings.\n\n"
                f"{question}")
    if not first:
        return f"[From Visor · the user is looking at {where}]\n\n{question}"
    if item.get("agent_provider") == "codex":
        return ("[Figure chat from Visor] This is a separate read-only fork of the original conversation. "
                f"The user is looking at {shown}. Discuss and explain this figure or run; "
                "treat earlier tasks as context, not work to continue. Do not edit files or take actions in external services. "
                "Answer briefly in the user's language with little Markdown.\n\n" + question)
    return ("[Message sent from Visor] This conversation is a fork of your session, opened from the Visor window. "
            f"The user is looking at {shown}. In this fork you can only "
            "read and search (Read, Grep, Glob): you cannot edit files or run commands. Answer in the user's language, "
            "briefly and with little Markdown, because the window shows plain text.\n\n"
            f"{question}")


def events(line):
    """Turn one stream-json line into (kind, text) pairs: session, text, tool, error, done."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return []
    kind = data.get("type")
    if kind == "system" and data.get("subtype") == "init":
        return [("session", data.get("session_id", ""))]
    if kind == "assistant":
        found = []
        for block in data.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                found.append(("text", block["text"].strip()))
            elif block.get("type") == "tool_use":
                arguments = block.get("input", {})
                target = Path(arguments["file_path"]).name if arguments.get("file_path") else arguments.get("pattern", "")
                found.append(("tool", f"{block.get('name', 'Herramienta')} {target}".strip()))
        return found
    if kind == "result":
        if data.get("is_error") or data.get("subtype") != "success":
            return [("error", str(data.get("result") or data.get("subtype") or "The session ended with an error.")), ("done", "")]
        return [("done", "")]
    return []
