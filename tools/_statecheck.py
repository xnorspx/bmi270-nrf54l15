"""Check the state fields the header binds to (address / connecting / recording)."""

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


def pump(session: CollectorSession, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        session.pump()
        time.sleep(0.05)


def main() -> int:
    s = CollectorSession(Path.cwd() / "sessions")
    s.start()

    print("initial   :", {k: s.stats()[k] for k in ("connected", "connecting",
                                                    "address", "recording")})
    s.connect()
    pump(s, 0.1)
    print("connecting:", {k: s.stats()[k] for k in ("connected", "connecting",
                                                    "address", "recording")})

    deadline = time.time() + 20
    while time.time() < deadline and not s.ble.connected:
        pump(s, 0.1)
    print("connected :", {k: s.stats()[k] for k in ("connected", "connecting",
                                                    "address", "recording")})

    s.start_recording()
    pump(s, 0.6)
    rec = s.stats()
    print("recording :", {k: rec[k] for k in ("connected", "connecting",
                                              "address", "recording")})
    s.stop_recording()

    ok = (rec["connected"] and not rec["connecting"] and rec["recording"]
          and rec["address"] not in ("", "-") and ":" in rec["address"])
    print()
    print("RESULT:", "state fields OK" if ok else "state fields wrong")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
