#!/usr/bin/env python3
"""
Native Linux driver for the Risemode RM-SCP-B "Smart Screen 9.2" USB panel
(VID 0x2100 / PID 0x0003, sold as "RT Systems HOTSPOTEKUSB HID DEMO").

Protocol reverse-engineered from a USB capture of the official Windows app:
  - Single HID interrupt OUT endpoint (0x01, 1024 byte packets) carries commands.
  - Panel is 462 x 1920, image expected as a plain JPEG rotated 180 degrees.
  - A frame is sent as: 32-byte header + JPEG bytes, chunked into 1024-byte
    packets (last packet zero-padded).
  - A CONNECT keep-alive must be resent roughly every 10 seconds or the panel
    drops the session and blanks.
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
import sys
import threading
import time

import usb.core
import usb.util

from panel_render import render_stats_image

VENDOR_ID = 0x2100
PRODUCT_ID = 0x0003
EP_OUT = 0x01
PACKET_SIZE = 1024
CONNECT_INTERVAL_S = 8
FRAME_INTERVAL_S = 0.3

CONNECT_PACKET = bytes.fromhex("4352540000434f4e4e454354") + b"\x00" * (
    PACKET_SIZE - 12
)


def build_brightness_packet(value):
    header = bytearray(13)
    header[0:4] = b"CRT\x00"
    header[4] = 0x00
    header[5:8] = b"LIG"
    header[8] = 0x00
    header[9] = 0x00
    header[10] = 0x00
    header[11] = value
    header[12] = 0x00
    return bytes(header) + b"\x00" * (PACKET_SIZE - 13)


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


def find_device():
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        raise ValueError("Risemode device not found (2100:0003)")

    # This firmware accumulates bad internal state across repeated
    # claim/release cycles (e.g. handing the device to/from a VM). A plain
    # USB bus reset clears it. find_device() is only called once at startup
    # and then reactively if a write actually fails (see main()) - it used
    # to also be called every 20s as a proactive "safety net" against a
    # wedge that in practice never seemed to actually happen under a
    # continuous, single claimed session (only after repeated claim/release,
    # e.g. VM/host handoff during development). That safety net was itself
    # the cause of a visible ~1s blackout every 20s, which the real Windows
    # driver doesn't do - it just holds one continuous session. Removed;
    # see the "Keeping the session alive" note in the README if a genuine
    # long-uptime wedge ever resurfaces.
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


def poll_in_endpoint(dev, stop_event):
    while not stop_event.is_set():
        try:
            dev.read(0x82, 512, timeout=50)
        except usb.core.USBError:
            pass


def run():
    dev = find_device()
    stop_event = threading.Event()
    poller = threading.Thread(target=poll_in_endpoint, args=(dev, stop_event), daemon=True)
    poller.start()

    try:
        print("Device claimed. Sending CONNECT...")
        dev.write(EP_OUT, CONNECT_PACKET)
        time.sleep(0.2)
        last_connect = time.time()
        frame_num = 0
        print("Streaming (Ctrl+C to stop)...")
        while True:
            now = time.time()
            if now - last_connect > CONNECT_INTERVAL_S:
                dev.write(EP_OUT, CONNECT_PACKET)
                last_connect = now
            jpeg = render_stats_image()
            send_frame(dev, jpeg)
            frame_num += 1
            time.sleep(FRAME_INTERVAL_S)
    finally:
        stop_event.set()
        poller.join(timeout=1)
        try:
            usb.util.release_interface(dev, 0)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(dev)


def main():
    while True:
        try:
            run()
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except (usb.core.USBError, ValueError) as e:
            print(f"USB error: {e}. Retrying in 3s...")
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
