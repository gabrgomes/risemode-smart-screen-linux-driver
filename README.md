# risemode-smart-screen-linux-driver

Unofficial native Linux driver for the **Risemode RM-SCP-B "Smart Screen 9.2"** USB secondary display panel (1920x462 IPS strip), sold as an official Windows-only accessory with no Linux/macOS support.

The device enumerates as:

```
ID 2100:0003 RT Systems HOTSPOTEKUSB HID DEMO
```

This is a generic/commodity HID controller board (unrelated to the well-known "Turing Smart Screen" family, which uses a different vendor ID and bulk-transfer protocol). This driver was built by reverse-engineering USB traffic captured from the official Windows application ("Rise Global USA").

## What it does

Runs as a background service that renders live system and game-performance stats directly onto the panel — no Windows, no VM required. Available sensors: CPU usage, CPU temperature, RAM usage, GPU usage/temperature, GPU VRAM usage, GPU power draw, FPS/1% low, frame time, and a clock. The panel background defaults to your current desktop wallpaper (auto-detected, center-cropped to the panel's portrait aspect ratio, and dimmed for text legibility). A settings GUI (`risemode_gui.py`) lets you pick a different background image and toggle sensors on/off, with a live preview, and applies instantly to the running service — see [Settings GUI](#settings-gui).

## Protocol notes

The panel exposes a single HID interface with one interrupt OUT endpoint (`0x01`, 1024-byte packets). Frames are sent as a 32-byte header followed by a plain JPEG (462x1920, rotated 180 degrees), chunked into 1024-byte packets.

Header layout:

```
0-3   "CRT\0"            magic
4     0x00
5-7   "DRA"               command ("draw")
8     0x00
9-11  big-endian uint24   total length = 32 + len(jpeg)
12    0xB1                constant marker
13-31 0x00                padding
```

Two firmware quirks discovered during reverse engineering, both handled by the driver:

- Calling `SET_CONFIGURATION` when the device is already configured silently caps the display session to about a second. The driver only sets it if not already configured.
- A `CONNECT` handshake packet (`"CRT\0\0CONNECT"` zero-padded to 1024 bytes) must be resent roughly every 10 seconds or the panel drops the session and goes black, even though every USB transfer keeps completing successfully.

This cheap firmware also tends to accumulate bad internal state after repeated USB claim/release cycles (e.g. passing the device between a VM and the host, which happened a lot during reverse engineering). A plain USB bus reset (`usb.core.Device.reset()`) clears it.

### Keeping the session alive (not resetting every 20 seconds)

An earlier version of this driver, out of caution about the claim/release-cycle issue above, proactively tore the whole session down and reset the device every 20 seconds "as a safety net" — which meant a real, visible ~1s blackout every 20 seconds, indefinitely, by design. That's very likely what the real Windows driver *doesn't* do: it almost certainly just claims the device once and holds one continuous session for as long as the app runs, using the `CONNECT` keep-alive alone to keep the firmware happy.

The driver now does the same: `find_device()` (which includes the bus reset) only runs once at startup, and again reactively if a write actually fails — not on a timer. The wedge that motivated the original workaround was only ever observed after repeated claim/release, not during a long continuous session, so removing the proactive reset should have no downside under normal use. If a genuine long-uptime wedge does resurface, the fix is to add a much longer interval (hours, not seconds) rather than reintroducing a 20-second one, or to inspect what the IN endpoint (`0x82`, currently just polled and discarded in `poll_in_endpoint()`) actually reports — the firmware may signal its own health there.

Brightness control (`LIG` command) exists in the protocol but only produces a brief flash before reverting to the panel's own default — it does not appear to be a true persistent "set" on this firmware, so the driver does not use it.

## Requirements

- Linux, `libusb`
- A udev rule granting non-root access to the device:

  ```
  # /etc/udev/rules.d/99-risemode-screen.rules
  SUBSYSTEM=="usb", ATTR{idVendor}=="2100", ATTR{idProduct}=="0003", MODE="0660", GROUP="plugdev", TAG+="uaccess"
  ```

  Reload with `sudo udevadm control --reload-rules && sudo udevadm trigger`, and make sure your user is in the `plugdev` group.

- (Optional, for GPU usage/temp/VRAM/power) `nvidia-smi` on the PATH — NVIDIA only. Without it these sensors just don't draw (rather than showing a placeholder), same as the panel skipping the whole GPU section today when no GPU is found.
- (Optional, for CPU temperature) an `lm-sensors`-visible CPU chip (`k10temp` on AMD, `coretemp` on Intel) — install/configure `lm-sensors` if `psutil.sensors_temperatures()` comes back empty. Falls back to whatever sensor chip is first available if neither is found.
- (Optional, for FPS / 1% low / frame time) [MangoHud](https://github.com/flightlessmango/MangoHud) configured to log continuously. Without it these show `--`.
- (Optional, for the background image) GNOME. `get_wallpaper_path()` reads `org.gnome.desktop.background picture-uri`/`picture-uri-dark` via `gsettings`; on any other desktop (or if that returns nothing/a non-`file://` URI, e.g. a solid color) it silently falls back to a plain dark background unless you've set a custom image (see below).
- (Optional, for the settings GUI) Tkinter (`python3-tk`) — usually preinstalled alongside `python3` on Debian/Ubuntu; if `risemode_gui.py` fails with `ModuleNotFoundError: tkinter`, `sudo apt install python3-tk`.

### Background image

By default the panel background is your desktop wallpaper: read via `gsettings`, center-cropped to the panel's 462x1920 portrait aspect ratio (cropping the long axis rather than stretching/squashing it), scaled down, and blended with black (`BG_DIM_ALPHA`, out of 255, in `panel_render.py`) so the stat text stays legible over bright or busy photos. The [settings GUI](#settings-gui) can override it with any image file instead. It's decoded once and cached; `load_background()` only re-decodes it if the effective path or its mtime changes, not every frame.

### GPU FPS via MangoHud

The FPS/1% low/frame time shown on the panel are the *real*, live values for whatever game/GL app is currently running, read from MangoHud's own CSV logging — not the driver's own frame-send rate. `get_game_stats()` in `panel_render.py` tails the newest non-summary CSV in `~/.local/share/mangohud_logs` and parses the whole latest row (keyed by MangoHud's own column names, whose order isn't hardcoded since it depends on MangoHud's config/version) rather than just pulling out FPS, so any other column MangoHud logs (`cpu_load`, `gpu_vram_used`, `swap_used`, ...) is available the same way if you want to wire up more sensors later.

```
sudo apt install mangohud
```

Getting this working reliably under Steam/Proton required working around a few real bugs/quirks, documented here so future-me doesn't have to rediscover them:

- **`no_display` disables logging entirely, not just the overlay.** This is a known upstream bug ([MangoHud#1782](https://github.com/flightlessmango/MangoHud/issues/1782), unresolved) — the overlay has to stay technically "on" for the metrics/logging code path to run at all. Workaround: keep it on but make it unobtrusive with `fps_only` (just the number, no graph/CPU/GPU clutter), a tiny `font_size`, no background box, and — since this MangoHud version has no alpha-channel/color-transparency support and the small logging-indicator dot it draws has a hardcoded, non-configurable 10px radius — pushing the whole HUD off-screen with a large negative `offset_x`/`offset_y` (e.g. `-3000`) so it renders outside the visible viewport instead of relying on transparency.
- **A manually-built MangoHud (`mangohud-setup.sh`, v0.8.4) may not read `~/.config/MangoHud/MangoHud.conf` at all** — no "parsing config" log output ever appears despite the file existing with correct content/permissions, and this couldn't be pinned down further. Workaround: set `MANGOHUD_CONFIG` as an environment variable instead — it's read directly by the layer and takes priority over any file, sidestepping the issue.
- **Steam's Proton sandbox (`pressure-vessel`) doesn't expose a manually-installed MangoHud** (`/usr/lib/mangohud`, as opposed to the apt package's standard system path) or the log output directory into the game's filesystem view. Needs `PRESSURE_VESSEL_FILESYSTEMS_RO=/usr/lib/mangohud` and `PRESSURE_VESSEL_FILESYSTEMS_RW=<log dir>`.
- **Setting all of this globally via `~/.config/environment.d/` is unreliable** on at least this machine — `systemd --user`'s live environment kept silently reverting to stale values for reasons that couldn't be fully root-caused (not purely a login-timing issue; it happened without any new login too). Per-game Steam Launch Options don't have this problem, since the shell that launches the game evaluates the env-var prefix fresh on every single launch.

The working config, applied as Steam Launch Options (not `environment.d`):

```
MANGOHUD_CONFIG=output_folder=/home/YOUR_USER/.local/share/mangohud_logs,autostart_log=1,log_duration=999999,log_interval=200,fps_only,font_size=10,hud_no_margin,background_alpha=0,alpha=0.15,position=top-left,text_color=000000,fps_color_change=0,engine_color=000000,offset_x=-3000,offset_y=-3000 PRESSURE_VESSEL_FILESYSTEMS_RO=/usr/lib/mangohud PRESSURE_VESSEL_FILESYSTEMS_RW=/home/YOUR_USER/.local/share/mangohud_logs mangohud %command%
```

Setting this by hand for every game is tedious, so a separate helper script (`add_mangohud.py`, not part of this repo) bulk-applies — and re-applies, after tweaking the config above — this wrapper to every installed game's `localconfig.vdf` entry in one run. Steam must be fully closed first.

OpenGL apps aren't covered by the Vulkan implicit layer and need an explicit wrapper too, e.g. `mangohud glxgears`.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python3 risemode_driver.py
```

## Settings GUI

```bash
./venv/bin/python3 risemode_gui.py
```

Lets you pick a background image (or fall back to the live desktop wallpaper) and toggle which sensors (CPU, RAM, GPU, FPS/1% low, clock) are shown, with a live preview of exactly what the panel would render. **Apply** just writes `~/.config/risemode-screen/config.json` — the running `risemode-screen` service picks it up on its very next frame (`get_config()` in `panel_render.py` is cached by mtime and reloads automatically), no restart needed.

`risemode_driver.py` (the USB protocol/streaming loop) and `risemode_gui.py` (the settings window) both render frames through the shared `panel_render.py`, so the GUI's preview and the panel's actual output are always pixel-for-pixel the same.

## Running as a systemd user service

```ini
# ~/.config/systemd/user/risemode-screen.service
[Unit]
Description=Risemode RM-SCP-B smart screen driver
After=graphical-session.target

[Service]
Type=simple
ExecStart=/path/to/risemode-smart-screen-linux-driver/venv/bin/python3 -u /path/to/risemode-smart-screen-linux-driver/risemode_driver.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now risemode-screen.service
```

If the panel ever goes dark and doesn't self-recover (the driver reconnects reactively when a write actually fails, see [Keeping the session alive](#keeping-the-session-alive-not-resetting-every-20-seconds)), `systemctl --user restart risemode-screen` clears it.

## Debugging tools

- `single_frame_test.py <jpeg>` — sends one JPEG frame and idles, useful for checking how long a single frame is held before the panel's watchdog blanks it.
- `replay_test.py <jpeg>` — replays a captured/generated JPEG frame repeatedly, useful for isolating protocol vs. rendering issues.

## License

MIT
