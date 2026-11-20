"""Headless end-to-end test of the collector core (no UI toolkit involved)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from collector import CollectorSession  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def pump(session: CollectorSession, seconds: float) -> int:
    got = 0
    end = time.time() + seconds
    while time.time() < end:
        got += len(session.pump())
        time.sleep(0.05)
    return got


def main() -> int:
    session = CollectorSession(Path.cwd() / "sessions")
    session.start()

    print("connecting ...")
    session.connect()
    deadline = time.time() + 20
    while time.time() < deadline and not session.ble.connected:
        session.pump()
        time.sleep(0.05)
    if not session.ble.connected:
        print("FAIL: could not connect")
        for line in session.drain_log():
            print("  ", line)
        return 1
    print("connected")

    session.set_rate(200)
    preview = pump(session, 1.5)
    print(f"preview rows before recording: {preview}")
    if preview < 100:
        print("FAIL: no live stream before recording")
        return 1

    print("recording ...")
    session.start_recording()
    recorded = pump(session, 2.5)

    session.add_mark("heel_strike", "左腳落地 測試")
    pump(session, 0.6)
    session.add_mark("toe_off", "")
    pump(session, 0.4)

    d = session.stop_recording()
    pump(session, 0.3)

    # marks stay editable after the file was closed
    session.update_mark(0, "heel_strike", "左腳落地（修正後備注）")
    session.update_mark(1, "toe_off", "右腳離地")

    if d is None:
        print("FAIL: nothing recorded")
        return 1

    csv_lines = (d / "imu.csv").read_text(encoding="utf-8").strip().splitlines()
    marks = [json.loads(x) for x in
             (d / "marks.jsonl").read_text(encoding="utf-8").strip().splitlines()]
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))

    print()
    print(f"session      : {d.name}")
    print(f"pumped rows  : {recorded}")
    print(f"csv rows     : {len(csv_lines) - 1}")
    print(f"header       : {csv_lines[0]}")
    print(f"marks        : {len(marks)}")
    for m in marks:
        print(f"   [{m['label']}] {m['note']}  @ sample {m['sample_index']}")
    print(f"meta         : rate={meta['rate_hz']} samples={meta['samples']} "
          f"dropped={meta['dropped_device']} gaps={meta['seq_gaps']}")

    ok = (len(csv_lines) > 300 and len(marks) == 2
          and marks[0]["note"] == "左腳落地（修正後備注）"
          and meta["samples"] > 300 and meta["seq_gaps"] == 0)
    print()
    print("RESULT:", "core pipeline OK" if ok else "check the numbers above")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
