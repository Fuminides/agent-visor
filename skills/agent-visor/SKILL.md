---
name: agent-visor
description: Show generated images, plots and screenshots in the user's local Visor window while working from the terminal. Use when visual results help the user assess the work, or they ask to see an image or compare visual versions.
---

# Visor

Use the installed local command to show useful visual results as part of the work. The user wants the agent to open the viewer automatically; invoke it yourself when showing a plot, image, screenshot, or a visual revision helps the task.

```bash
~/.local/bin/visor show /absolute/path/to/result.png --title "Distribution by group" --caption "The two groups show different variance."
```

The command starts the window when closed and reuses it when open. It returns after the window receives the image; it selects it when “Seguir lo nuevo” is enabled. Continue working after the call; no manual app launch or background server setup is needed. PDF, PNG, JPEG, WebP, GIF, TIFF and BMP are supported; GIFs show their first frame. Export SVG plots to PNG or PDF before showing them. The original file stays untouched, and the viewer saves an immutable copy.

The working directory is the default project/history group. Call from the user's project directory, or use `--session "project / conversation"` consistently when separate conversations need separate histories. Multiple paths can be sent in one call; the last is selected. Repeating a show call records a new version.

```bash
~/.local/bin/visor show before.png after.png --session "Study / comparison"
~/.local/bin/visor status --json
```

## Presentations

For a set of related figures meant to be read in order (a results walkthrough, a talk, before/after steps), send one presentation instead of many separate images. Either show a multi-page PDF (Beamer, Quarto, Marp, matplotlib `PdfPages`…), which becomes one history entry with one slide per page, or join several images with `--deck`:

```bash
~/.local/bin/visor show /abs/path/talk.pdf --title "Ablation results" --caption "Slide 3 has the main comparison."
~/.local/bin/visor show /abs/a.png /abs/b.png /abs/c.png --deck --title "Training summary"
```

The user turns slides with ←/→ or PgUp/PgDn. A call accepts up to 100 images or pages. Regenerating the PDF adds a new version and keeps the user on the same slide. Title and caption apply to the whole presentation.

## Questions from the viewer

When run from Claude Code, `visor show` records the session that showed the image (from `CLAUDE_CODE_SESSION_ID`); no flag is needed. The user can then ask about that image in Visor's side panel, which answers from a read-only fork of this session (Read, Grep, Glob only) and leaves this session untouched. Clear titles and captions help the fork identify the figure being asked about.

The newest displayed source file is also watched while “Seguir lo nuevo” is enabled. Regenerating it updates the view and retains the old snapshot. Default updates do not raise or focus the existing window. Add `--focus` only when the user explicitly wants it brought to the front. Respect a user disabling “Seguir lo nuevo”; incoming images still enter history.

Show useful milestones and final outputs, rather than every intermediate frame. Choose titles and captions in the user's language. A successful show confirms delivery, not the user's approval of the image. With `--json`, `queued: true` means it entered history while the user kept a previous image selected. Do not claim the user has reviewed it. If the viewer cannot access a graphical desktop or an image is inaccessible locally, report the error and retain a normal file link as a fallback. An environment permission failure does not mean the app must be launched by the user; use the environment's normal permission mechanism where available.
