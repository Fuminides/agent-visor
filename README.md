# Visor

A local window where terminal coding agents such as Claude Code and Codex show you images, plots and presentations. The `visor` command opens the window when it is needed and reuses it when it is already open. You can also ask the Claude Code session that made a figure about it, right from the window.

## Usage

```bash
visor show plot.png --title "Results" --caption "The error drops after tuning."
visor show before.png after.png --session "My analysis"
visor show talk.pdf --title "Experiment results"
visor show s1.png s2.png s3.png --deck --title "Summary"
visor demo
visor status --json
visor history --limit 10
visor stop
```

By default the current folder identifies the project. Set `VISOR_SESSION` or pass `--session` to keep conversations apart. The history stores copies, so overwriting or deleting the original file does not remove earlier versions.

## Presentations

A multi-page PDF is stored as a single history entry, with its pages as slides in order. Any generator works: Beamer, Quarto, Marp, matplotlib's `PdfPages`… With `--deck`, every image and PDF in the same call becomes one presentation, titled after the first file. Without `--deck`, each image is its own entry. One call accepts up to 100 images or pages. PDF pages are rendered to PNG at 2400 px on the long side.

If you regenerate the PDF (or any file of the presentation) while **Follow latest** is on, a new version comes in and the window stays on the same slide.

## Ask the session

When Claude Code shows an image, `visor show` also records which session showed it (`CLAUDE_CODE_SESSION_ID`). The **Ask** button opens a side panel where you can ask that session about the selected image or presentation without interrupting it.

- The first question creates a fork (`claude --resume <id> --fork-session`). Later questions continue that fork, so it remembers what you discussed. **New thread** starts another fork.
- Right-click the figure to mark a point. The next question carries its coordinates and two images that the model opens with `Read`: the figure with the point circled in red, and a zoom around it. They are saved in `~/.local/share/agent-visor/points/`.
- **Copy command** copies `claude --resume <fork>`, so you can continue that conversation in a terminal.
- While it is answering, **Send** becomes **Stop**.

The fork can only read and search (`Read`, `Grep`, `Glob`, no MCP servers). It cannot edit files or run commands, because nobody can approve permissions from the window. The original session is left unchanged. The fork copies the session as it is when you ask, so it also knows what happened after the image was shown. Each version of a figure has its own thread. Images that did not come from Claude Code (from Codex, or opened by hand) have no session to ask.

## Recent history

The **All**, **Today** and **Last hour** buttons under HISTORY filter entries by date. Hidden entries stay saved, and the chosen filter is remembered.

## Updates and focus

The selected image refreshes when its source file changes, as long as it is the latest version of that file and **Follow latest** is on. The change must stay stable for two checks (about two seconds). Turn the option off to look through versions while the agent keeps sending results. The new-images button takes you to the latest one.

The first call opens the window. Later calls update it without raising it or taking focus; `--focus` brings it to the front. Closing the window stops the app. The next image opens it again and restores the history. Nothing runs at startup.

## Controls

- Mouse wheel: zoom around the pointer. Drag: pan.
- Double-click or `F`: fit the image. `1`: actual size.
- Left/right arrows: previous/next slide in a presentation, and previous/next version for other images.
- Page Up/Page Down: previous/next slide (works with presentation clickers).
- Up/down arrows and the ← → buttons: newer/older history entry.
- `Ctrl+O`: open images by hand.
- Right-click on the image: mark a point to ask about the element there. Opens the question panel if it is closed.
- In the question panel: `Enter` sends and `Shift+Enter` adds a line. While you type, the window shortcuts are off; click the image to get them back.
- **EN | ES** in the header switches the interface between English and Spanish.

Visor reads PDF, PNG, JPEG, WebP, GIF (first frame), TIFF, BMP and ICO, up to 24 megapixels or 100 MB per file. Export SVG to PNG or PDF first. The window lists up to 200 recent entries per project; older ones stay saved and can be listed with `visor history --limit 1000`.

## Installation

Linux with a graphical desktop, Python 3.10+, Tk and Pillow. PDFs also need `poppler-utils` (`pdfinfo` and `pdftoppm`), which most distributions ship. The question panel needs the `claude` command (Claude Code). The installer uses the libraries already on the system and downloads nothing.

```bash
python3 install.py
~/.local/bin/visor demo
```

The installer creates the `~/.local/bin/visor` command, an application menu entry, and links to the skill in `~/.codex/skills/agent-visor` and `~/.claude/skills/agent-visor`. New sessions of both agents can discover it and use it when showing something visual helps. Whether a skill is picked up depends on the agent; the installer does not change their permissions or configure MCP. In a session that is already open, the agent can read `skills/agent-visor/SKILL.md` directly.

Keep this folder where it is, since the command and the skill link to it. If you move it, run the installer again and update the existing links. `~/.local/bin` must be on `PATH` to use the short name; the absolute path always works.

## Data

- Images and history: `~/.local/share/agent-visor/`.
- Error log: `~/.local/share/agent-visor/visor.log`.
- IPC: a private Unix socket in `$XDG_RUNTIME_DIR/agent-visor/`, or `~/.cache/agent-visor/`.
- No network ports, accounts or API keys. Only the question panel calls a model, through your `claude` command, and only when you send a question.

XDG directories are respected. `VISOR_DATA_DIR` and `VISOR_RUNTIME_DIR` isolate data and processes, for example during tests. The history stays on disk until you delete it.

## Development

```bash
xvfb-run -a python3 -m unittest discover -s tests -v
```

The window tests open many short-lived windows, so run them on a virtual display (`xvfb-run`, from the `xvfb` package) to keep them off your desktop. Without any display they are skipped. With a desktop they exercise the real command, simultaneous startup, reuse, updates, closing and restoring. If you have Codex's skill-creator, its `quick_validate.py` checks `skills/agent-visor`.

To uninstall, run `visor stop` and delete the command, the two skill links and `~/.local/share/applications/agent-visor.desktop`. The data is kept separately, so you can keep it.

## License

MIT. See [LICENSE](LICENSE).
