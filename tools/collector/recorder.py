"""Session recorder: one directory per recording, thread-safe.

    imu.csv     t_dev_us,t_host_ns,ax_mg,ay_mg,az_mg,gx_mdps,gy_mdps,gz_mdps
    marks.jsonl one JSON object per mark point, rewritten from memory on edit
    meta.json   rate, counts, gaps, timing
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from .protocol import ACC_LSB_PER_G, GYR_LSB_PER_DPS, DEVICE_NAME

CSV_HEADER = "t_dev_us,t_host_ns,ax_mg,ay_mg,az_mg,gx_mdps,gy_mdps,gz_mdps\n"


class Recorder:
    def __init__(self) -> None:
        self.dir: Path | None = None
        self._csv = None
        self.marks: list[dict] = []
        self.samples = 0
        self.dropped = 0
        self.seq_gaps = 0
        self.rate_hz = 100
        self.started_at = 0.0
        self._last_seq: int | None = None
        self._last_dev_us = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._csv is not None

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    def start(self, root: Path, rate_hz: int) -> Path:
        with self._lock:
            stamp = datetime.now().strftime("session_%Y%m%d_%H%M%S")
            self.dir = root / stamp
            self.dir.mkdir(parents=True, exist_ok=True)
            self._csv = open(self.dir / "imu.csv", "w", encoding="utf-8", newline="")
            self._csv.write(CSV_HEADER)
            self.marks = []
            self.rate_hz = rate_hz
            self.samples = 0
            self.dropped = 0
            self.seq_gaps = 0
            self._last_seq = None
            self.started_at = time.monotonic()
            return self.dir

    def add_frame(self, seq: int, dropped: int, rows: list[tuple]) -> None:
        if not self.active:
            return
        with self._lock:
            if self._last_seq is not None and ((seq - self._last_seq) & 0xFFFF) != 1:
                self.seq_gaps += 1
            self._last_seq = seq
            self.dropped = max(self.dropped, dropped)

            host_ns = time.time_ns()
            for t_us, ax, ay, az, gx, gy, gz in rows:
                self._csv.write(
                    f"{t_us},{host_ns},"
                    f"{ax * 1000 / ACC_LSB_PER_G:.3f},{ay * 1000 / ACC_LSB_PER_G:.3f},"
                    f"{az * 1000 / ACC_LSB_PER_G:.3f},"
                    f"{gx * 1000 / GYR_LSB_PER_DPS:.3f},{gy * 1000 / GYR_LSB_PER_DPS:.3f},"
                    f"{gz * 1000 / GYR_LSB_PER_DPS:.3f}\n"
                )
            if rows:
                self._last_dev_us = rows[-1][0]
            self.samples += len(rows)

    # -- mark points ------------------------------------------------------
    def add_mark(self, label: str, note: str) -> dict:
        with self._lock:
            entry = {
                "t_dev_us": self._last_dev_us,
                "t_host_ns": time.time_ns(),
                "t_wall": datetime.now().isoformat(timespec="milliseconds"),
                "label": label,
                "note": note,
                "sample_index": self.samples,
            }
            self.marks.append(entry)
            self._flush_marks()
            return entry

    def update_mark(self, index: int, label: str | None = None,
                    note: str | None = None) -> None:
        with self._lock:
            if not 0 <= index < len(self.marks):
                return
            if label is not None:
                self.marks[index]["label"] = label
            if note is not None:
                self.marks[index]["note"] = note
            self.marks[index]["t_wall"] = datetime.now().isoformat(timespec="milliseconds")
            self._flush_marks()

    def delete_mark(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self.marks):
                del self.marks[index]
                self._flush_marks()

    def _flush_marks(self) -> None:
        if self.dir is None:
            return
        with open(self.dir / "marks.jsonl", "w", encoding="utf-8") as fh:
            for entry in self.marks:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # -- shutdown ---------------------------------------------------------
    def stop(self) -> Path | None:
        with self._lock:
            if self.dir is None:
                return None
            d = self.dir
            if self._csv is not None:
                try:
                    self._csv.flush()
                    self._csv.close()
                except Exception:
                    pass
                self._csv = None
            self._flush_marks()
            meta = {
                "device_name": DEVICE_NAME,
                "rate_hz": self.rate_hz,
                "samples": self.samples,
                "dropped_device": self.dropped,
                "seq_gaps": self.seq_gaps,
                "marks": len(self.marks),
                "duration_s": round(self.duration, 3),
                "units": {"accel": "mg", "gyro": "mdps", "t_dev_us": "device uptime us"},
                "accel_range_g": 8,
                "gyro_range_dps": 500,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
            (d / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
            return d
