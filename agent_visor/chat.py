"""Ask the Claude Code session that showed a figure, in a read-only fork of it. No GUI imports here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil

SESSION_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
# The fork can look at the project but never change it: no Bash, no edits, no MCP servers.
READ_ONLY_TOOLS = "Read,Grep,Glob"


def claude_binary():
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local/bin/claude"
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("No se encuentra el comando claude.")


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


def command(session_id, fork_id, prompt):
    """First question forks the original session; later ones continue that fork."""
    args = [claude_binary(), "-p", "--resume", fork_id or session_id]
    if not fork_id:
        args.append("--fork-session")
    return args + ["--tools", READ_ONLY_TOOLS, "--strict-mcp-config", "--permission-mode", "dontAsk",
                   "--output-format", "stream-json", "--verbose", prompt]


def prompt(question, item, first):
    where = f"«{item['title']}» ({item['source']}; copia guardada en {item['snapshot']})"
    if item.get("deck"):
        where += f", diapositiva {item['page']} de {item['pages']}"
    if not first:
        return f"[Desde Visor · el usuario mira {where}]\n\n{question}"
    return ("[Mensaje enviado desde Visor] Esta conversación es una bifurcación de tu sesión, abierta desde la ventana "
            f"de Visor. El usuario está viendo la figura {where}, que mostraste con `visor show`. En esta bifurcación "
            "solo puedes leer y buscar (Read, Grep, Glob): no puedes editar archivos ni ejecutar comandos. Responde "
            "en el idioma del usuario, de forma breve y con poco Markdown, porque la ventana muestra texto plano.\n\n"
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
                target = arguments.get("file_path") or arguments.get("pattern") or ""
                found.append(("tool", f"{block.get('name', 'Herramienta')} {target}".strip()))
        return found
    if kind == "result":
        if data.get("is_error") or data.get("subtype") != "success":
            return [("error", str(data.get("result") or data.get("subtype") or "La sesión terminó con un error.")), ("done", "")]
        return [("done", "")]
    return []
