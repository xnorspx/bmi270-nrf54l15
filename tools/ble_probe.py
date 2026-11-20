"""Connect to the collector and verify the BLE sample stream end to end.

Also serves as the reference decoder for the binary framing:

    frame:  [type u8][seq u16le][dropped u16le][n u8][sample...]
    sample: [t_us u32le][ax i16][ay i16][az i16][gx i16][gy i16][gz i16]
"""

from __future__ import annotations

import asyncio
import statistics
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

SVC = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # host -> device (write)
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # device -> host (notify)
TARGET = "KneeChe IMU"

ACC_LSB_PER_G = 4096.0
GYR_LSB_PER_DPS = 65.536


def decode_frame(payload: bytes) -> dict | None:
    if len(payload) < 6 or payload[0] != 0x01:
        return None
    seq, dropped, n = struct.unpack_from("<HHB", payload, 1)
    expect = 6 + n * 16
    if len(payload) < expect:
        return None

    samples = []
    for i in range(n):
        t_us, ax, ay, az, gx, gy, gz = struct.unpack_from("<Ihhhhhh", payload, 6 + i * 16)
        samples.append((t_us, ax, ay, az, gx, gy, gz))
    return {"seq": seq, "dropped": dropped, "samples": samples}


async def main() -> int:
    print(f"scanning for {TARGET!r} ...")
    dev = await BleakScanner.find_device_by_name(TARGET, timeout=10.0)
    if dev is None:
        print("device not found")
        return 1
    print(f"found {dev.address}")

    frames: list[dict] = []

    def on_notify(_char, data: bytearray) -> None:
        f = decode_frame(bytes(data))
        if f:
            frames.append(f)

    async with BleakClient(dev, timeout=20.0) as client:
        print(f"connected, mtu={client.mtu_size}")
        await client.start_notify(TX_UUID, on_notify)

        # let the link settle, then ask for data
        await asyncio.sleep(0.3)
        if len(sys.argv) > 1:
            rate = sys.argv[1]
            await client.write_gatt_char(RX_UUID, f"S,{rate}\n".encode(), response=False)
            print(f"set rate to {rate} Hz")
            await asyncio.sleep(0.4)
        await client.write_gatt_char(RX_UUID, b"START\n", response=False)
        print("sent START, collecting 3s ...")
        t0 = time.monotonic()
        await asyncio.sleep(3.0)
        await client.write_gatt_char(RX_UUID, b"STOP\n", response=False)
        await asyncio.sleep(0.3)
        elapsed = time.monotonic() - t0
        await client.stop_notify(TX_UUID)

    if not frames:
        print("no frames received")
        return 2

    samples = [s for f in frames for s in f["samples"]]
    ts = [s[0] for s in samples]
    deltas = [b - a for a, b in zip(ts, ts[1:]) if 0 < b - a < 1_000_000]
    dropped = max(f["dropped"] for f in frames)
    seq_gaps = sum(1 for a, b in zip(frames, frames[1:]) if (b["seq"] - a["seq"]) & 0xFFFF != 1)

    print()
    print(f"frames      : {len(frames)}  ({', '.join(str(len(f['samples'])) for f in frames[:6])} samples/frame ...)")
    print(f"samples     : {len(samples)} in {elapsed:.2f}s -> {len(samples)/elapsed:.1f} Hz")
    if deltas:
        print(f"sample dt   : median {statistics.median(deltas):.0f} us, "
              f"min {min(deltas)}, max {max(deltas)}")
    print(f"device drop : {dropped}")
    print(f"seq gaps    : {seq_gaps}")

    a = [s[1:4] for s in samples[-50:]]
    g = [s[4:7] for s in samples[-50:]]
    mx = sum(x[0] for x in a) / len(a) / ACC_LSB_PER_G
    my = sum(x[1] for x in a) / len(a) / ACC_LSB_PER_G
    mz = sum(x[2] for x in a) / len(a) / ACC_LSB_PER_G
    mag = (mx * mx + my * my + mz * mz) ** 0.5
    print(f"accel mean  : {mx:+.3f} {my:+.3f} {mz:+.3f} g   |a| = {mag:.3f} g")

    ok = len(samples) / elapsed > 80.0 and mag > 0.8 and mag < 1.2
    print()
    print("RESULT:", "stream looks healthy" if ok else "stream is off - check the numbers above")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
