# Prompt for Claude Code on the Linux machine

Copy this folder into the driver repo (e.g. as `docs/windows-capture/`), open Claude Code in the repo root, and paste
everything below the line.

---

I'm working on my Linux driver for the Risemode smart screen (USB VID 0x2100 / PID 0x0003), in this repo. Right now it
only keeps the panel alive by reconnecting and doing a USB bus reset every few seconds (`SESSION_MAX_S` in
`risemode_driver.py`). On Windows, the official app runs for minutes with no resets.

I captured the Windows app's USB traffic with USBPcap and analyzed it. Everything is in `docs/windows-capture/`:
- `PROTOCOL.md`: the protocol as the Windows app actually speaks it (commands, byte layout, timing)
- `windows-command-timeline.tsv`: every command/header report Windows sent, with timestamps
- `windows-startup-47s.pcapng`: the first 47 s of the capture (device idle, then app startup, then 3 CONNECTs).
  You can inspect it with `tshark` if it's installed, e.g.
  `tshark -r windows-startup-47s.pcapng -Y 'usb.endpoint_address==0x01 && usb.irp_info.direction==0' -T fields -e frame.time_relative -e usbhid.data`

Read `PROTOCOL.md` first. Frame framing (the DRA header, the `+32` size, 1024-byte chunks, zero padding) already
matches Windows exactly, so don't change it. These are the differences found when comparing the capture with
`risemode_driver.py`:

1. **The driver never sends `DIS`. This is the most likely cause.** Windows' very first command is
   `CRT\0\0DIS` (`43 52 54 00 00 44 49 53`, zero-padded to 1024), then `LIG`, then frames. Its first `CONNECT` only
   comes ~10 s after `DIS`. Our driver sends `CONNECT` first and never `DIS`. In the capture, the device sends
   `00 00` on IN endpoint 0x82 every 3 s while idle, and stops as soon as the app's session starts. So `DIS`
   seems to switch the firmware out of idle/standby mode. That fits our symptom: "renders ~1-3 s then goes dark
   while every transfer succeeds."
2. **`SESSION_MAX_S = 5`** forces a reconnect and a `dev.reset()` every 5 s. Windows never resets or reconnects. The
   README says 20 s, which is out of date.
3. **`build_brightness_packet` puts the value at byte 11, but Windows puts it at byte 10**:
   `43 52 54 00 00 4C 49 47 00 00 32 00…` (0x32 = 50). The function is currently unused (brightness is done in
   software).
4. **`poll_in_endpoint` reads 0x82 with a 50 ms timeout**, which cancels and resubmits ~20 times a second. Windows
   keeps one read pending, and it never completes during streaming. Probably harmless, but it differs.

Windows startup timing: DIS at t0 → LIG(50) at +0.39 s → first DRA at +0.47 s → frames every ~62 ms (~16 fps).
Reports within a frame are sent back-to-back. CONNECT every 10.000 s, always between frames. Commands are never
interleaved inside a frame.

What I want you to do:
1. Keep the current behavior available (a git branch or a `--legacy` flag), because `ROADMAP.md` says the
   reset/reconnect workaround was "confirmed necessary". We're testing whether that's still true once we match Windows.
2. Change `risemode_driver.py` to mimic Windows:
   - send `DIS`, wait ~0.4 s, send `LIG` with the value at byte 10 (fix `build_brightness_packet`), wait ~0.08 s, then
     start frames
   - first CONNECT ~10 s after DIS, then every 10 s, only between frames
   - make the proactive reconnect configurable, and disable it for this test
   - keep `dev.reset()` only for the first startup / error recovery, not for periodic reconnects
   - keep `set_configuration()` avoidance as is
3. Add a small standalone test script (like `single_frame_test.py`) that does DIS → LIG → streams a simple
   changing test image (e.g. a counter) for 5 minutes with no reconnects. It should log any USB errors, and also log
   whether the device still sends `00 00` on 0x82 after DIS. That tells us whether DIS was accepted.
4. Tell me exactly how to run it (stop the `risemode-screen` systemd user service first so it doesn't hold the
   device), and what to watch for on the panel.
5. If the panel still goes dark, help me capture the Linux traffic with `usbmon` + Wireshark/tshark
   (`sudo modprobe usbmon`, find the bus with `lsusb`) and diff it against `windows-startup-47s.pcapng`. Compare
   first commands, report sizes, and timing.

Don't update the README/ROADMAP claims about the firmware wedge until the 5-minute test has passed.
