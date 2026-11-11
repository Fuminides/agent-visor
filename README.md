# Visor

A local window where terminal coding agents such as Claude Code and Codex show you images, plots and presentations, and where you can watch a training or evaluation while it runs. The `visor` command opens the window when it is needed and reuses it when it is already open. You can also ask the agent session that made a figure about it, right from the window.

## Usage

```bash
visor show plot.png --title "Results" --caption "The error drops after tuning."
visor show before.png after.png --session "My analysis"
visor show talk.pdf --title "Experiment results"
visor show s1.png s2.png s3.png --deck --title "Summary"
visor track run.log --title "PPO on HalfCheetah" --metrics reward,loss
visor follow workstation        # on a laptop: show here what agents show on that computer
visor demo
visor status --json
visor history --limit 10
visor stop
```

By default the current folder identifies the project. Set `VISOR_SESSION` or pass `--session` to keep conversations apart. The history stores copies, so overwriting or deleting the original file does not remove earlier versions.

## Presentations

A multi-page PDF is stored as a single history entry, with its pages as slides in order. Any generator works: Beamer, Quarto, Marp, matplotlib's `PdfPages`… With `--deck`, every image and PDF in the same call becomes one presentation, titled after the first file. Without `--deck`, each image is its own entry. One call accepts up to 100 images or pages. PDF pages are rendered to PNG at 2400 px on the long side.

If you regenerate the PDF (or any file of the presentation) while **Follow latest** is on, a new version comes in and the window stays on the same slide.

## Live runs

`visor track FILE` follows the file a training or evaluation writes its progress to, and adds the run to the history as a live entry. Its page shows how far the run is, the time left and when it should end, one chart per metric, the latest value of anything else the lines report, and the notes the agent leaves. The window reads the file by itself, every 2 seconds, so the page keeps moving for hours with no agent awake.

```bash
nohup python -u train.py > run.log 2>&1 &
visor track run.log --title "PPO on HalfCheetah" --metrics reward,loss --note "Baseline, 3 seeds"
visor track run.log --note "Seed 2 diverged at step 20k"      # later: adds a note to the same run
visor track peanut:/home/me/project/run.log --title "Sweep"   # a file on another machine, read over ssh
```

The script needs no library. A line counts when it has one of these forms; every other line of the log is ignored:

```
[3/12] seed=0 acc=0.91 loss=0.12              3 of 12 done, then key=value pairs
 25%|██▌       | 3/12 [00:10<00:30, 3.3s/it]   a tqdm bar (pairs in its postfix count too)
{"step": 3, "total": 12, "acc": 0.91, "phase": "eval"}
```

- `step` and `total` give the progress. Count the outermost unit of the whole job (runs, seeds × folds, total environment steps), not the batches of one epoch.
- Each number becomes a curve over the step, up to 12 curves. `--metrics reward,loss` chooses which; the other keys, and every value that is not a number, appear as their latest value (`seed 0 · phase eval`).
- The time left is measured from the pace seen while following, so it appears once two steps have gone by after `visor track`. What the file already held does not count.
- `--note` adds a dated remark under the charts. Calling `visor track` again for the same file updates that run and does not add another.
- The command answers with what it understood (`Following · step 3/12 · curves: acc, loss`), so a log in a format Visor cannot read is noticed at once.

`host:/absolute/path` reads the file with `ssh host` every 10 seconds, using your ssh configuration in batch mode: it never asks for a password, so the host must accept your key. While the host cannot be reached the page says so, and it catches up when the connection is back.

When a new launch writes to the same file from the start, the earlier run stays in the history as it was and the new one gets its own entry. A run stops being followed one minute after its last step, or after 12 hours without a new line; `visor track` on it follows it again. Point at a curve to read the value at a step, and scroll with the wheel when the page is longer than the window. **Ask** works on a run as on a figure: the fork gets what Visor has read from the file.

## Ask the session

When Codex or Claude Code shows an image or follows a run, `visor show` and `visor track` record the originating agent session. **Ask** opens a side panel for the selected entry.

**Codex: choose Figure chat or Original chat.** The two buttons at the top of Ask switch between:

- **Figure chat** creates a separate read-only fork with the original context. Ask about a curve, a marked point, a slide, or a run without adding messages to the ongoing conversation. Further questions continue that fork; **New thread** starts another. **Stop** stops only the figure fork. This mode can use stored context even after the original session has stopped, as long as the local Codex server is available.
- **Original chat** sends directly to the session that showed the entry, while it is still loaded on the shared local Codex server. You can discuss the wider task or request a plot change there. The thread keeps its model, tools and permissions; handle approval requests in Codex. **Detach** stops following a reply without interrupting the agent. **Reconnect** checks availability again.

Each mode keeps its own message history and draft when you switch, and Visor remembers the selected mode for each entry. Switching modes leaves answers in progress running. Figure forks use a read-only filesystem sandbox with approvals disabled; they do not continue goals inherited from the original conversation. Original chat never reopens an inactive original session.

Visor records `CODEX_THREAD_ID` (or `CODEX_SESSION_ID`) and the local control socket. This requires a recent Codex with a shared local app server; standalone `--no-daemon` and remote sessions are not connected automatically. `VISOR_CODEX_SOCKET` can select a custom local control socket when showing an entry.

**Claude Code: ask in a read-only fork.** The first question creates a fork (`claude --resume <id> --fork-session`); later questions continue it. **New thread** starts another fork. The fork can only read and search (`Read`, `Grep`, `Glob`, no MCP servers), and leaves the original session unchanged. **Stop** stops the fork's answer.

Right-click a figure to mark a point. The next question includes its coordinates, a saved copy of that exact figure version with the point circled, and a zoom. Slides also include the selected page number. **Copy command** copies a terminal command for the connected Codex thread or the Claude fork. Each entry keeps its question history.

Older Codex entries have no recorded session; show the image or track the run again from its original session to connect it. Images opened by hand have no session to ask.

## Projects and conversations

The button at the top of the sidebar names the project being shown. Click it to browse every conversation Visor has stored, grouped by project and most recently used first, each with its latest image, how many figures it holds and when it last changed. Type to filter by any part of the name, move with the up/down arrows, press `Enter` to open one and `Esc` to go back.

A conversation is one `--session` value, or one folder. `--session "Study / comparison"` is the conversation "comparison" of the project "Study". A folder is filed under its git repository, so a subfolder or a linked worktree (`git worktree`) appears as a conversation of the main repository's project, next to that project's named conversations.

## Recent history

The history of a conversation is listed newest first under a heading per day. The **All**, **Today** and **Last hour** buttons under HISTORY filter entries by date. Hidden entries stay saved, and the chosen filter is remembered.

## Updates and focus

The selected image refreshes when its source file changes, as long as it is the latest version of that file and **Follow latest** is on. The change must stay stable for two checks (about two seconds). Turn the option off to look through versions while the agent keeps sending results. The new-images button takes you to the latest one.

The first call opens the window. Later calls update it without raising it or taking focus; `--focus` brings it to the front. Closing the window stops the app. The next image opens it again and restores the history. Nothing runs at startup.

## Controls

- Mouse wheel: zoom around the pointer (on a live run: scroll its page). Drag: pan.
- Double-click or `F`: fit the image. `1`: actual size.
- Left/right arrows: previous/next slide in a presentation, and previous/next version for other images.
- Page Up/Page Down: previous/next slide (works with presentation clickers).
- Up/down arrows and the ← → buttons: newer/older history entry.
- `Ctrl+O`: open images by hand.
- `S` or the button at the left of the header: close or reopen the sidebar. The ✕ at the top of the question panel (or **Ask**) closes that panel. With both closed the figure has the whole window; the choice is remembered.
- Right-click on the image: mark a point to ask about the element there. Opens the question panel if it is closed.
- In the question panel: `Enter` sends and `Shift+Enter` adds a line. While you type, the window shortcuts are off; click the image to get them back.
- **EN | ES** in the header switches the interface between English and Spanish.
- The four dots in the header choose the colours of the window: sand (warm, the default), stone (grey and white), sky (bluish) and dark. The choice is remembered.

## Accessibility

- Every control can be reached with `Tab` and shows a focus ring with at least 3:1 contrast; text meets 4.5:1.
- Sizes follow the screen's scale (Tk scaling), so the layout keeps its proportions on HiDPI screens.
- Visor is built with Tk, which has no screen-reader (AT-SPI) support on Linux, so screen readers cannot read the window.

Visor reads PDF, PNG, JPEG, WebP, GIF (first frame), TIFF, BMP and ICO, up to 24 megapixels or 100 MB per file. Export SVG to PNG or PDF first. The window lists up to 200 recent entries per project; older ones stay saved and can be listed with `visor history --limit 1000`.

## Another computer

When the agents run on a workstation and you sit at a laptop, the laptop's window can show what they show. On the laptop:

```bash
visor follow workstation         # a host name from your ssh config
visor status --json              # "mirrors": {"workstation": "connected"}, or what went wrong
visor follow workstation --off
```

The agents change nothing: they keep calling `visor show` and `visor track` on the workstation, which stores each entry in its history as always. The laptop's window keeps one `ssh workstation` connection open, over which the workstation sends every new entry, figure included, and files it in the laptop's own history as it arrives. Figures and presentations appear as when shown locally (**Follow latest**, versions, slides); a live run is read from the laptop with `ssh`, like `visor track workstation:/path`.

- Both computers need Visor installed, and the laptop must reach the workstation with `ssh workstation`. The connection only ever goes from the laptop to the workstation, so the laptop can be behind any network.
- When ssh needs a password, a verification code or the confirmation of a computer it has not seen before, Visor asks in a dialog; the answer goes to ssh and is not kept. `visor follow` waits for that and says how it went. That one connection also serves the readings of the workstation's live runs, so you are asked once per connection. If the answer does not connect (or you cancel), Visor does not ask again by itself: run `visor follow workstation` when you want another try.
- The entries are filed as their own projects, named `project @ workstation`, so they do not mix with what the laptop shows for a folder of the same name. Their detail line gives the file's place on the workstation.
- The choice is remembered: the window follows the workstation again whenever it opens. After a break (lid closed, network change) it reconnects by itself every 15 seconds, asking for the password again if ssh needs it, and continues after the last entry received; when more than 20 entries are waiting, the 20 newest come.
- `visor show` on the workstation still opens its own window there, so the workstation needs its graphical session as before.
- Not carried over yet: **Ask** for those entries (their agent session lives on the workstation), the notes of a run, and runs whose file is on a third computer.

## Installation

Linux with a graphical desktop, Python 3.10+, Tk and Pillow. PDFs also need `poppler-utils` (`pdfinfo` and `pdftoppm`), which most distributions ship. The question panel uses the local Codex server for Codex entries, or the `claude` command for Claude Code entries. The installer uses the libraries already on the system and downloads nothing.

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
- No network ports, accounts or API keys. Only the question panel sends questions to an agent, through your local Codex server or `claude` command, and only when you send a question. `visor follow` opens an outgoing `ssh` connection to the computer you name and nothing else.

XDG directories are respected. `VISOR_DATA_DIR` and `VISOR_RUNTIME_DIR` isolate data and processes, for example during tests. The history stays on disk until you delete it.

## Development

```bash
xvfb-run -a python3 -m unittest discover -s tests -v
```

The window tests open many short-lived windows, so run them on a virtual display (`xvfb-run`, from the `xvfb` package) to keep them off your desktop. Without any display they are skipped. With a desktop they exercise the real command, simultaneous startup, reuse, updates, closing and restoring. If you have Codex's skill-creator, its `quick_validate.py` checks `skills/agent-visor`.

To uninstall, run `visor stop` and delete the command, the two skill links and `~/.local/share/applications/agent-visor.desktop`. The data is kept separately, so you can keep it.

## License

MIT. See [LICENSE](LICENSE).
