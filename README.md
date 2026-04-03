# Focus Border

`Focus Border` is a small background app for X11 desktops. It polls the active window and draws a transparent, click-through border around it so the focused window is easier to spot.

## Requirements

- `python3`
- `PyGObject` with GTK 3
- `xprop`
- `xwininfo`

## Run

```bash
python3 focus_border.py
```

Useful flags:

- `--color ff5f57`
- `--alpha 0.95`
- `--thickness 4`
- `--radius 10`
- `--inset 6`
- `--poll-ms 120`

Example:

```bash
python3 focus_border.py --color 00b894 --thickness 6 --radius 14
```

## Start At Login

Install the desktop entry into your autostart folder:

```bash
python3 focus_border.py --install-autostart
```

That copies [io.github.sander.FocusBorder.desktop](/home/sander/tmp/2026-04-03/io.github.sander.FocusBorder.desktop) into `~/.config/autostart/`.

## Notes

- This targets X11. It will not work on native Wayland-only sessions.
- Fullscreen windows are ignored so the overlay does not sit on top of full-screen apps or videos.
- The desktop entry currently points at this checkout: [focus_border.py](/home/sander/tmp/2026-04-03/focus_border.py)
