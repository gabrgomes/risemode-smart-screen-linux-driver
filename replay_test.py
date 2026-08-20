#!/usr/bin/env python3
"""Replay a known-good captured JPEG frame to test our transport logic in isolation."""
import sys
import time

import usb.core
import usb.util

VENDOR_ID = 0x2100
PRODUCT_ID = 0x0003
EP_OUT = 0x01
PACKET_SIZE = 1024

CONNECT_PACKET = bytes.fromhex("4352540000434f4e4e454354") + b"\x00" * (
    PACKET_SIZE - 12
)


def find_device():
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        raise ValueError("Risemode device not found (2100:0003)")
    for cfg in dev:
        for intf in cfg:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    return dev


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


def send_frame(dev, jpeg_bytes):
    payload = build_draw_header(len(jpeg_bytes)) + jpeg_bytes
    pad_len = (-len(payload)) % PACKET_SIZE
    payload += b"\x00" * pad_len
    for i in range(0, len(payload), PACKET_SIZE):
        dev.write(EP_OUT, payload[i : i + PACKET_SIZE])


def main():
    jpeg_path = sys.argv[1] if len(sys.argv) > 1 else "/home/gabriel/vms/frame1.jpg"
    jpeg_bytes = open(jpeg_path, "rb").read()
    print(f"Loaded {jpeg_path}: {len(jpeg_bytes)} bytes")

    dev = find_device()
    print("Device claimed. Sending CONNECT...")
    dev.write(EP_OUT, CONNECT_PACKET)
    time.sleep(0.2)

    print("Replaying captured frame 20 times...")
    for i in range(20):
        send_frame(dev, jpeg_bytes)
        time.sleep(0.15)
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
