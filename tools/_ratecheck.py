"""Diagnose the sustained sample rate: cumulative vs instantaneous, and loss."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from collector import CollectorSession  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> int:
    hz = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0

    s = CollectorSession(Path.cwd() / "sessions")
    s.start()
    s.connect()
    deadline = time.time() + 20
    while time.time() < deadline and not s.ble.connected:
        s.pump()
        time.sleep(0.05)
    if not s.ble.connected:
        print("FAIL: no connection")
        return 1

    s.set_rate(hz)
    time.sleep(0.3)
    s.start_recording()
    print(f"recording at {hz} Hz for {seconds:.0f}s")
    print(f"{'t':>5} {'total':>7} {'inst/s':>7} {'cum Hz':>8} {'drop':>6} {'gaps':>5}")
    t0 = time.time()
    last_total = 0
    last_t = t0
    while time.time() - t0 < seconds:
        s.pump()
        now = time.time()
        if now - last_t >= 1.0:
            st = s.stats()
            inst = (st["samples"] - last_total) / (now - last_t)
            print(f"{now - t0:5.1f} {st['samples']:7d} {inst:7.1f} "
                  f"{st['rate_inst']:8.1f} {st['dropped']:6d} {st['gaps']:5d}")
            last_total = st["samples"]
            last_t = now
        time.sleep(0.05)

    d = s.stop_recording()
    st = s.stats()
    print(f"\nfinal: samples={st['samples']} cumulative={st['measured_hz']:.1f} Hz "
          f"dropped={st['dropped']} gaps={st['gaps']} dir={d.name if d else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
