"""Validate the sliding-window rate with a synthetic feed (no hardware needed).

Two phases, because the rate must now be live while merely previewing:
  1. connected but NOT recording  -> rate_inst and live_samples must still move
  2. recording                    -> recorded samples start counting too
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from collector import CollectorSession  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

INJECT_HZ = 200.0
PERIOD = 1.0 / INJECT_HZ


def feed(s: CollectorSession, seconds: float, counter: list[int], t0: float,
         label: str) -> None:
    end = time.time() + seconds
    last = 0.0
    while time.time() < end:
        now = time.time() - t0
        if now >= counter[0] * PERIOD:
            n = counter[0]
            s.ble.events.put(("samples", (n & 0xFFFF, 0,
                                          [(n * 5000, 100, 200, 4096, 10, 20, 30)])))
            counter[0] += 1
        s.pump()
        if now - last >= 1.0:
            st = s.stats()
            print(f"  {label} t={now:4.1f}s  live={st['live_samples']:5d}  "
                  f"rec={st['samples']:5d}  rate_inst={st['rate_inst']:6.1f} Hz")
            last = now
        time.sleep(0.001)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    s = CollectorSession(tmp)
    s.ble.connected = True          # pretend the BLE link is up
    t0 = time.time()
    counter = [0]

    print(f"phase 1: connected, NOT recording (injecting {INJECT_HZ:.0f} Hz)")
    feed(s, 2.5, counter, t0, "preview")
    st1 = s.stats()
    print(f"  -> preview rate {st1['rate_inst']:.1f} Hz, live {st1['live_samples']}")

    print(f"phase 2: recording")
    s.rec.start(tmp, int(INJECT_HZ))
    feed(s, 2.0, counter, t0, "record ")
    st2 = s.stats()
    print(f"  -> record rate {st2['rate_inst']:.1f} Hz, recorded {st2['samples']}")

    s.rec.stop()

    ok_preview = 180 <= st1["rate_inst"] <= 220 and st1["live_samples"] > 300
    ok_record = 180 <= st2["rate_inst"] <= 220 and st2["samples"] > 300
    print()
    print(f"preview rate works : {ok_preview}")
    print(f"record  rate works : {ok_record}")
    print("RESULT:", "live rate OK" if (ok_preview and ok_record) else "rate window wrong")
    return 0 if (ok_preview and ok_record) else 1


if __name__ == "__main__":
    sys.exit(main())
