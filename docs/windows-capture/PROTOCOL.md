# Rise Mode USB smart display — protocol notes (from Windows capture)

Capture: `captures/risemode-20260926-051401_00001_20260926051403.pcapng`
(USBPcap, device address 2 only; 339 s; app started at t=13.6 s; theme changed a few times.)

Device: VID 0x2100 / PID 0x0003, "HOTSPOTEKUSB HID DEMO", vendor-defined HID.
App: "Rise Global USA" — a Mirabox **StreamDock** build (plugins under `%APPDATA%\HotSpot\Rise Global USA\plugins`).

## Endpoints
| EP   | Dir | Type      | Use |
|------|-----|-----------|-----|
| 0x01 | OUT | interrupt | everything the app sends: 1024-byte reports |
| 0x82 | IN  | interrupt | 2-byte `00 00` every 3.000 s while no host app is talking; **stops** once the app starts |

No feature reports, no SET_REPORT on EP0, no reads from the device during streaming.
Startup on EP0 is only GET_DESCRIPTOR (strings), which is HID enumeration by the app.
All 278,203 OUT transfers completed with status 0.

Reports are 1024 bytes on the wire with no report ID. **On Linux hidraw, write 1025 bytes with a leading `0x00`**
(or use libusb interrupt transfers to EP 0x01 with exactly 1024 bytes).

## Command report layout (1024 bytes, zero-padded)
```
off 0..4   43 52 54 00 00        "CRT\0\0"
off 5..    command (ASCII) + args
```

| Command | Bytes (hex, from offset 0) | Meaning |
|---------|----------------------------|---------|
| DIS     | `43 52 54 00 00 44 49 53`  | display on / wake. First thing sent. |
| LIG     | `43 52 54 00 00 4C 49 47 00 00 32` | brightness, byte 10 = 0x32 (50) |
| CONNECT | `43 52 54 00 00 43 4F 4E 4E 45 43 54` | keep-alive |
| DRA     | see below | draw full-screen JPEG |

## DRA (frame) layout
Header report:
```
off 0..7   "CRT\0\0DRA"
off 8..11  size, uint32 BIG-endian  = 32 + jpeg_len   <-- includes the 32-byte header!
off 12     0xB1 (constant in every frame)
off 13..31 zero
off 32..   first 992 bytes of the JPEG (starts FF D8 FF E0 ... JFIF)
```
Then continuation reports: the raw JPEG bytes continue, 1024 per report, with no header. The last one is zero-padded.
**Total reports per frame = ceil(size / 1024)**, where size = jpeg_len + 32. Checked against all 5,219 frames.
Frame sizes seen: 14.5 KB – 81 KB (avg 54 KB).

## Timing (Windows)
- t0: DIS
- +0.39 s: LIG 50
- +0.47 s: first DRA; then frames every ~62 ms (~16 fps; min gap 12 ms, max 211 ms)
- Reports inside a frame are sent back-to-back (~0.12 ms apart), with no waiting on the device.
- CONNECT: **every 10.000 s** (±5 ms), first one ~10 s after DIS. It's always sent **between** frames, never inside one.
- Commands are never interleaved inside a frame's continuation stream.

## Checklist for the Linux driver
1. Send `CONNECT` every 10 s, between frames only.
2. Set the size field to `jpeg_len + 32`, big-endian, and send exactly `ceil(size/1024)` reports.
   An off-by-32 size, or an extra or missing report, leaves the device expecting the wrong number of bytes. The next
   header then gets read as image data. With ~3% of frames ending within 32 bytes of a report boundary,
   that would go wrong every couple of seconds at 16 fps.
3. Byte 12 = 0xB1.
4. Never send a command (CONNECT, LIG, …) in the middle of a frame. Serialize everything through one writer.
5. hidraw: prepend the 0x00 report-ID byte (1025-byte writes).
6. Send DIS, then LIG, before the first frame.
7. Disable USB autosuspend for 2100:0003 (Windows keeps the IN endpoint polled, so the device never suspends).
