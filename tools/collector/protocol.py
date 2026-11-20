"""Wire format for the BLE sample pipe and the unit conversions.

    frame:  [type u8][seq u16le][dropped u16le][n u8][sample...]
    sample: [t_us u32le][ax i16][ay i16][az i16][gx i16][gy i16][gz i16]

Raw LSB values travel over the air; conversion to engineering units happens
here so the firmware never rounds anything.
"""

from __future__ import annotations

import struct

SVC_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
DEVICE_NAME = "KneeChe IMU"

FRAME_SAMPLES = 0x01
FRAME_STATUS = 0x02

SAMPLE_SIZE = 16
FRAME_HDR = 6

ACC_LSB_PER_G = 4096.0     # +-8 g range
GYR_LSB_PER_DPS = 65.536   # +-500 dps range

RATES = [25, 50, 100, 200, 400]


def decode(payload: bytes) -> tuple[int, int, list[tuple]] | None:
    """Return (seq, dropped, samples) or None if the frame is not sample data."""
    if len(payload) < FRAME_HDR or payload[0] != FRAME_SAMPLES:
        return None
    seq, dropped, n = struct.unpack_from("<HHB", payload, 1)
    if len(payload) < FRAME_HDR + n * SAMPLE_SIZE:
        return None
    rows = [struct.unpack_from("<Ihhhhhh", payload, FRAME_HDR + i * SAMPLE_SIZE)
            for i in range(n)]
    return seq, dropped, rows


def to_engineering(row: tuple) -> tuple[float, float, float, float, float, float]:
    """(ax, ay, az) in g, (gx, gy, gz) in deg/s."""
    _, ax, ay, az, gx, gy, gz = row
    return (ax / ACC_LSB_PER_G, ay / ACC_LSB_PER_G, az / ACC_LSB_PER_G,
            gx / GYR_LSB_PER_DPS, gy / GYR_LSB_PER_DPS, gz / GYR_LSB_PER_DPS)
