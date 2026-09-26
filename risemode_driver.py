#!/usr/bin/env python3
"""
Native Linux driver for the Risemode RM-SCP-B "Smart Screen 9.2" USB panel
(VID 0x2100 / PID 0x0003, sold as "RT Systems HOTSPOTEKUSB HID DEMO").

Protocol reverse-engineered from a USB capture of the official Windows app:
  - Single HID interrupt OUT endpoint (0x01, 1024 byte packets) carries commands.
  - Panel is 462 x 1920, image expected as a plain JPEG rotated 180 degrees.
  - A frame is sent as: 32-byte header + JPEG bytes, chunked into 1024-byte
    packets (last packet zero-padded).
  - A session must open with DIS (then LIG, then frames), like the official
    Windows app does: DIS takes the firmware out of idle mode. Without it the
    panel stays dark while every write "succeeds" - that was the long-standing
    "firmware wedge", not a real one. See docs/windows-capture/PROTOCOL.md.
  - A CONNECT keep-alive is resent every 10 seconds.
  - Do not call set_configuration() when the device is already configured -
    on this firmware it silently caps the session to about a second.

Header layout (32 bytes):
  0-3   "CRT\0"                  magic
  4     0x00
  5-7   "DRA"                    command name ("draw")
  8     0x00
  9-11  big-endian uint24        total length = 32 (header) + len(jpeg)
  12    0xB1                     constant marker
  13-31 0x00                     padding
"""
import argparse
import sys
import threading
import time
from dataclasses import dataclass

import usb.core
import usb.util

from panel_render import render_stats_image

VENDOR_ID = 0x2100
PRODUCT_ID = 0x0003
EP_OUT = 0x01
EP_IN = 0x82
PACKET_SIZE = 1024
CONNECT_INTERVAL_S = 10


def build_command(name, *args):
    """A 1024-byte command report: "CRT\\0\\0" + ASCII name + raw arg bytes,
    zero-padded (see docs/windows-capture/PROTOCOL.md)."""
    header = b"CRT\x00\x00" + name + bytes(args)
    return header + b"\x00" * (PACKET_SIZE - len(header))


CONNECT_PACKET = build_command(b"CONNECT")
DIS_PACKET = build_command(b"DIS")  # display on / wake - Windows' first command


def build_brightness_packet(value):
    """LIG: the brightness value is byte 10 (`43 52 54 00 00 4C 49 47 00 00 32`
    in the Windows capture, 0x32 = 50). Not used by default - the panel
    doesn't seem to keep it, so brightness is done in software (panel_render)."""
    return build_command(b"LIG", 0x00, 0x00, value)


def build_draw_header(jpeg_len):
    total_len = jpeg_len + 32
    header = bytearray(32)
    header[0:4] = b"CRT\x00"
    header[4] = 0x00
    header[5:8] = b"DRA"
    header[8] = 0x00
    header[9:12] = total_len.to_bytes(3, "big")
    header[12] = 0xB1
    return bytes(header)


@dataclass(frozen=True)
class Mode:
    """How a streaming session is run. WINDOWS copies the official app's
    traffic pattern (the default). LEGACY is the original driver's pattern -
    no DIS, so it needs the reconnect/reset cycle to show anything for long;
    kept only for comparison."""
    name: str
    send_dis: bool               # DIS -> LIG -> frames startup, vs. CONNECT first
    lig_value: int
    first_connect_delay_s: float  # windows: first CONNECT this long after DIS
    reconnect_after_s: float      # proactive session teardown/reconnect; 0 = never
    reset_on_reconnect: bool      # USB bus reset on such a periodic reconnect
    frame_interval_s: float
    fixed_rate: bool              # frames start every frame_interval_s (windows), vs.
                                  # sleeping frame_interval_s after each one (legacy)
    in_read_timeout_ms: int       # how long each read of the IN endpoint waits


LEGACY = Mode(
    name="legacy", send_dis=False, lig_value=0, first_connect_delay_s=0.2,
    # without DIS the panel goes dark after a second or two while writes keep
    # succeeding; a fresh reset briefly brings it back, hence the timer
    reconnect_after_s=5, reset_on_reconnect=True,
    frame_interval_s=0.15, fixed_rate=False, in_read_timeout_ms=50,
)
WINDOWS = Mode(
    name="windows", send_dis=True, lig_value=0x32,
    first_connect_delay_s=CONNECT_INTERVAL_S,
    reconnect_after_s=0, reset_on_reconnect=False,
    frame_interval_s=0.062, fixed_rate=True, in_read_timeout_ms=2000,
)
MODES = {m.name: m for m in (LEGACY, WINDOWS)}
DEFAULT_MODE = "windows"  # proven stable (see docs/windows-capture); "legacy" is the
                          # old workaround, kept only for comparison

# Windows' startup timing: (GET_REPORT) -0.08s-> DIS -0.39s-> LIG -0.08s-> frames
GET_REPORT_TO_DIS_S = 0.08
DIS_TO_LIG_S = 0.39
LIG_TO_FIRST_FRAME_S = 0.08


def find_device(reset=True):
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        raise ValueError("Risemode device not found (2100:0003)")

    if reset:
        # This firmware accumulates bad internal state across repeated
        # claim/release cycles (e.g. handing the device to/from a VM). A plain
        # USB bus reset clears it; without this the panel will render for only
        # ~1-3 seconds before going dark, even though every USB transfer keeps
        # completing successfully. Done on first startup and after errors -
        # in "windows" mode not for periodic reconnects.
        dev.reset()
        time.sleep(0.5)
        dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)

    for cfg in dev:
        for intf in cfg:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)

    # Calling set_configuration() when already configured also truncates
    # the session to ~1 second on this firmware - only call it if needed.
    try:
        dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()

    usb.util.claim_interface(dev, 0)
    return dev


def send_frame(dev, jpeg_bytes):
    payload = build_draw_header(len(jpeg_bytes)) + jpeg_bytes
    pad_len = (-len(payload)) % PACKET_SIZE
    payload += b"\x00" * pad_len
    for i in range(0, len(payload), PACKET_SIZE):
        dev.write(EP_OUT, payload[i : i + PACKET_SIZE])


def read_firmware_string(dev):
    """The HID GET_REPORT (input, id 0) Windows issues just before DIS - the
    device answers with a version string (e.g. "V25.RM_SCP92.02.011").
    Best effort: returns the printable text, or None."""
    try:
        data = dev.ctrl_transfer(0xA1, 0x01, 0x0100, 0, 512, timeout=1000)
    except usb.core.USBError:
        return None
    text = bytes(data).decode("ascii", "ignore")
    return "".join(c for c in text if c.isprintable()) or None


def open_session(dev, mode):
    """Everything sent before the first frame. Returns the time the session's
    keep-alive schedule counts from."""
    if not mode.send_dis:
        dev.write(EP_OUT, CONNECT_PACKET)
        time.sleep(mode.first_connect_delay_s)
        return time.time()

    read_firmware_string(dev)
    time.sleep(GET_REPORT_TO_DIS_S)
    dev.write(EP_OUT, DIS_PACKET)
    dis_time = time.time()
    time.sleep(DIS_TO_LIG_S)
    dev.write(EP_OUT, build_brightness_packet(mode.lig_value))
    time.sleep(LIG_TO_FIRST_FRAME_S)
    return dis_time


def poll_in_endpoint(dev, stop_event, timeout_ms, on_data=None):
    """Keeps a read pending on the IN endpoint (the device sends `00 00` every
    3s while idle). Windows leaves one read outstanding the whole time; the
    timeout here is just how often it's cancelled and re-issued."""
    while not stop_event.is_set():
        try:
            data = dev.read(EP_IN, 512, timeout=timeout_ms)
        except usb.core.USBError:
            continue
        if on_data:
            on_data(time.time(), bytes(data))


def run(mode, reset):
    dev = find_device(reset=reset)
    stop_event = threading.Event()
    poller = threading.Thread(
        target=poll_in_endpoint, args=(dev, stop_event, mode.in_read_timeout_ms), daemon=True)
    poller.start()

    try:
        print(f"Device claimed ({mode.name} mode). Opening session...")
        session_start = open_session(dev, mode)
        last_connect = session_start
        next_frame = time.time()
        print("Streaming (Ctrl+C to stop)...")
        while True:
            now = time.time()
            if mode.reconnect_after_s and now - session_start > mode.reconnect_after_s:
                print("Proactive periodic reconnect...")
                return
            # only ever between frames - never inside one
            if now - last_connect >= CONNECT_INTERVAL_S:
                dev.write(EP_OUT, CONNECT_PACKET)
                last_connect = now
            send_frame(dev, render_stats_image())
            next_frame = (next_frame if mode.fixed_rate else time.time()) + mode.frame_interval_s
            delay = next_frame - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_frame = time.time()  # running behind - don't try to catch up
    finally:
        stop_event.set()
        poller.join(timeout=mode.in_read_timeout_ms / 1000 + 1)
        try:
            usb.util.release_interface(dev, 0)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(dev)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Risemode smart screen driver")
    parser.add_argument("--mode", choices=sorted(MODES), default=DEFAULT_MODE,
                        help="legacy: CONNECT first + reconnect/reset every few seconds; "
                             "windows: DIS/LIG startup, ~16 fps, CONNECT every 10s, no "
                             f"periodic reconnect (default: {DEFAULT_MODE})")
    parser.add_argument("--legacy", action="store_const", dest="mode", const="legacy",
                        help="shorthand for --mode legacy")
    parser.add_argument("--reconnect-after", type=float, metavar="SECONDS",
                        help="proactively reconnect this often; 0 disables (mode default "
                             "otherwise: legacy 5, windows 0)")
    parser.add_argument("--reset-on-reconnect", choices=("yes", "no"),
                        help="USB bus reset on periodic reconnects (mode default otherwise)")
    parser.add_argument("--frame-interval", type=float, metavar="SECONDS",
                        help="target time between frames (mode default otherwise)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    mode = MODES[args.mode]
    overrides = {}
    if args.reconnect_after is not None:
        overrides["reconnect_after_s"] = args.reconnect_after
    if args.reset_on_reconnect is not None:
        overrides["reset_on_reconnect"] = args.reset_on_reconnect == "yes"
    if args.frame_interval is not None:
        overrides["frame_interval_s"] = args.frame_interval
    if overrides:
        mode = Mode(**{**mode.__dict__, **overrides})

    reset = True  # first startup always resets
    while True:
        try:
            run(mode, reset)
            reset = mode.reset_on_reconnect  # periodic reconnect
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except (usb.core.USBError, ValueError) as e:
            print(f"USB error: {e}. Retrying in 3s...")
            time.sleep(3)
            reset = True  # error recovery always resets


if __name__ == "__main__":
    sys.exit(main())
