# agent-visor

## Testing Visor: always on the virtual screen

Never open Visor windows on the user's desktop while developing or testing here. The window tests alone open
dozens of short-lived windows, and they flash over whatever the user is doing. Everything that starts the app
from this repo goes through `xvfb-run`, which gives it a virtual screen the user never sees.

- Tests (the whole suite or a single one):

  ```bash
  xvfb-run -a python3 -m unittest discover -s tests -v
  ```

  Never run `python3 -m unittest ...` bare: with the desktop's `DISPLAY` set, the window tests run on the real
  screen.

- Trying a change by hand or taking a screenshot: launch the repo's code on the virtual screen with its own data
  and runtime folders, so it does not touch the user's running Visor or its history. Everything has to happen
  inside the one `xvfb-run` call, because the virtual screen goes away when that command ends:

  ```bash
  T=$(mktemp -d /tmp/av-XXXX)   # keep it short: the runtime folder holds a Unix socket, and long paths are rejected
  VISOR_DATA_DIR=$T/data VISOR_RUNTIME_DIR=$T/run xvfb-run -a -s "-screen 0 1600x1000x24" bash -c \
    "python3 launch.py show /abs/image.png --title prueba; sleep 3; import -window root $T/shot.png; python3 launch.py stop"
  ```

  Read `$T/shot.png` yourself to check the result.

- The one window the user should see is a finished result you want them to look at: show that screenshot once
  with the installed command, `/home/javier/.local/bin/visor show`, as the global instructions say. Do not use
  the installed `visor` to exercise code under development.
