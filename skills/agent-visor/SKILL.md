---
name: agent-visor
description: Show generated images, plots and screenshots in the user's local Visor window while working from the terminal, and follow a long training or evaluation there as a live entry with progress, time left and metric curves. Use when visual results help the user assess the work, they ask to see an image or compare visual versions, or you launch a run that takes more than a few minutes.
---

# Visor

Use the installed local command to show useful visual results as part of the work. The user wants the agent to open the viewer automatically; invoke it yourself when showing a plot, image, screenshot, or a visual revision helps the task.

```bash
~/.local/bin/visor show /absolute/path/to/result.png --title "Distribution by group" --caption "The two groups show different variance."
```

The command starts the window when closed and reuses it when open. It returns after the window receives the image; it selects it when “Follow latest” is enabled. Continue working after the call; no manual app launch or background server setup is needed. PDF, PNG, JPEG, WebP, GIF, TIFF and BMP are supported; GIFs show their first frame. Export SVG plots to PNG or PDF before showing them. The original file stays untouched, and the viewer saves an immutable copy.

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

## Live runs: training and evaluation

When you launch something long (a training, an RL experiment, a sweep, an evaluation), follow its progress file so the user can watch it without asking you. Call it once, right after launching; the window then reads the file by itself every 2 seconds, also while you are idle.

```bash
nohup python -u train.py > run.log 2>&1 &
~/.local/bin/visor track /abs/path/run.log --title "PPO on HalfCheetah" --metrics reward,loss --note "Baseline, 3 seeds, lr 3e-4"
```

The script only has to print (flushed: `python -u` or `flush=True`) lines in one of these forms; the rest of the log is ignored:

```
[3/12] seed=0 acc=0.91 loss=0.12
{"step": 3, "total": 12, "acc": 0.91, "phase": "eval"}
```

A tqdm bar over the outer loop is read too. `step`/`total` (the `k/N`) must count the outermost unit of the whole job, from 1 to N; the time left is measured from them. Numbers become one chart each over the step; pass `--metrics` to choose the ones worth a chart (otherwise `seed=0` gets a chart too), and the remaining keys show their latest value. Text values work as status fields (`phase=eval`, `best=ckpt_12.pt`).

- Read the command's answer: `Following · step 3/12 · curves: acc, loss` confirms the lines are understood. `no progress line yet` on a log that already has output means the format is wrong: fix the print.
- Add what the user should know as it happens with `visor track <same file> --note "…"`: a divergence, a restart, the final result. The same file updates the same entry; notes are dated and listed newest first.
- A run on another machine: `visor track host:/abs/path/run.log`, with a host name from the user's ssh config. It is read over `ssh` in batch mode every 10 seconds, so it must be reachable from this computer without a password prompt.
- Relaunching into the same file (truncating it) starts a new entry by itself and keeps the old curves.
- The page shows what the file says. For the final figures (comparisons, tables, plots across runs) still use `visor show`.

## Questions from the viewer

When run from Codex, `visor show` and `visor track` record the exact originating thread (`CODEX_THREAD_ID`, or `CODEX_SESSION_ID`) and the local control socket. In the Ask panel, the user can toggle between **Figure chat** (a separate read-only fork with the original context) and **Original chat** (questions or plot changes sent to the original session while it is still loaded on the shared local Codex server). Each mode keeps its own history and draft; switching leaves answers in progress running. Figure chat can use stored context even if the original session has stopped, and New thread starts a fresh fork. Its model and permissions stay as configured; approvals remain in Codex. Detaching from an Original chat reply leaves the original agent running. Stop in Figure chat stops only its fork. Closing Visor leaves the original agent running. An inactive session must be opened in Codex before clicking Reconnect. Standalone `--no-daemon` and remote sessions are not connected automatically. For a custom local socket, set `VISOR_CODEX_SOCKET` when showing or tracking. Older Codex entries must be shown/tracked again from their original session to record this link.

When run from Claude Code, `visor show` and `visor track` record the session that showed the image or followed the run (from `CLAUDE_CODE_SESSION_ID`); no flag is needed. The user can then ask about that image in Visor's side panel, which answers from a read-only fork of this session (Read, Grep, Glob only) and leaves this session untouched. Clear titles and captions help the fork identify the figure being asked about.

The newest displayed source file is also watched while “Follow latest” is enabled. Regenerating it updates the view and retains the old snapshot. Default updates do not raise or focus the existing window. Add `--focus` only when the user explicitly wants it brought to the front. Respect a user disabling “Follow latest”; incoming images still enter history.

The user may be looking at another computer that follows this one (`visor follow`, set up by them there): what you show or track here also reaches their window there, with nothing to do on your side. Keep calling the local command as usual.

Show useful milestones and final outputs, rather than every intermediate frame. Choose titles and captions in the user's language. A successful show confirms delivery, not the user's approval of the image. With `--json`, `queued: true` means it entered history while the user kept a previous image selected. Do not claim the user has reviewed it. If the viewer cannot access a graphical desktop or an image is inaccessible locally, report the error and retain a normal file link as a fallback. An environment permission failure does not mean the app must be launched by the user; use the environment's normal permission mechanism where available.
