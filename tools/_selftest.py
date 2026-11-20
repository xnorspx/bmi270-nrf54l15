"""Self-test for the recorder output format (no BLE, no GUI)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector_ui as cu  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    rec = cu.Recorder()
    d = rec.start(tmp, 100, "KneeChe IMU")
    rec.add_frame(1, 0, [(1000, 100, -3900, -800, 10, -20, 5),
                         (11000, 110, -3910, -810, 11, -21, 6)])
    rec.add_frame(2, 0, [(21000, 105, -3905, -805, 9, -19, 4)])
    rec.add_mark("heel_strike", "左腳落地 第3次")
    rec.stop()

    print("session dir:", d.name)
    print("--- imu.csv ---")
    print((d / "imu.csv").read_text(encoding="utf-8").strip())
    print("--- marks.jsonl ---")
    print((d / "marks.jsonl").read_text(encoding="utf-8").strip())
    print("--- meta.json ---")
    print((d / "meta.json").read_text(encoding="utf-8").strip())

    import csv
    rows = list(csv.DictReader((d / "imu.csv").open(encoding="utf-8")))
    ok = len(rows) == 3 and abs(float(rows[0]["ay_mg"]) + 952.148) < 0.5
    print()
    print("RESULT:", "output format OK" if ok else "unexpected values")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
