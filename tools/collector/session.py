"""CollectorSession - everything the UI drives, with no UI toolkit imported.

Keeping the state machine out of the widget layer is what makes the pipeline
testable: the end-to-end test drives this class directly and then inspects the
files it produced.
"""

from __future__ import annotations

import queue
import time
from pathlib import Path

from .ble import BleSource
from .protocol import DEVICE_NAME, RATES, to_engineering
from .recorder import Recorder

MAX_DRAIN = 2000   # cap per pump so a burst can never stall the caller
RATE_WINDOW_S = 2.0   # sliding window for the instantaneous rate


class CollectorSession:
    def __init__(self, session_root: Path) -> None:
        self.session_root = session_root
        self.session_root.mkdir(parents=True, exist_ok=True)
        self.ble = BleSource()
        self.rec = Recorder()
        self.rate_hz = 100
        self.log: list[str] = []
        self.connecting = False
        self._t0 = time.monotonic()
        # (monotonic time, cumulative sample count) for the sliding-window rate
        self._rate_hist: list[tuple[float, int]] = []
        # counts every sample that arrives, recorded or not, so the panel can
        # show a live refresh rate while merely previewing
        self._sample_count = 0

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.ble.start()

    def shutdown(self) -> None:
        if self.rec.active:
            self.rec.stop()
        self.ble.disconnect()

    # -- commands ---------------------------------------------------------
    def connect(self) -> None:
        self.connecting = True
        self._say(f"connecting to {DEVICE_NAME} ...")
        self.ble.connect()

    def disconnect(self) -> None:
        self.ble.disconnect()

    def set_rate(self, hz: int) -> None:
        if hz not in RATES:
            raise ValueError(f"rate must be one of {RATES}")
        self.rate_hz = hz
        self.ble.command(f"S,{hz}")
        self._say(f"rate -> {hz} Hz")

    def start_recording(self) -> Path:
        if not self.ble.connected:
            raise RuntimeError("not connected")
        d = self.rec.start(self.session_root, self.rate_hz)
        self.ble.command(f"S,{self.rate_hz}")
        self._say(f"recording -> {d.name}")
        return d

    def stop_recording(self) -> Path | None:
        if not self.rec.active:
            return None
        samples, marks = self.rec.samples, len(self.rec.marks)
        d = self.rec.stop()
        self._say(f"saved {d.name}  ({samples} samples, {marks} marks)")
        return d

    # -- marks ------------------------------------------------------------
    def add_mark(self, label: str, note: str) -> dict | None:
        if not self.rec.active:
            self._say("mark ignored - not recording")
            return None
        entry = self.rec.add_mark(label or "event", note)
        self._say(f"mark #{len(self.rec.marks)}  [{entry['label']}]  "
                  f"{note or '(no note)'}")
        return entry

    def update_mark(self, index: int, label: str, note: str) -> None:
        self.rec.update_mark(index, label, note)
        self._say(f"mark #{index + 1} updated")

    def delete_mark(self, index: int) -> None:
        self.rec.delete_mark(index)
        self._say(f"mark #{index + 1} deleted")

    # -- data pump --------------------------------------------------------
    def pump(self) -> list[tuple]:
        """Drain pending BLE events. Returns the new rows as engineering units.

        The caller is expected to call this on a fixed tick (20 Hz in the UI),
        not per sample.
        """
        new_rows: list[tuple] = []
        drained = 0
        while drained < MAX_DRAIN:
            try:
                kind, payload = self.ble.events.get_nowait()
            except queue.Empty:
                break
            drained += 1

            if kind == "samples":
                seq, dropped, rows = payload
                if self.rec.active:
                    self.rec.add_frame(seq, dropped, rows)
                # (t_seconds, ax, ay, az, gx, gy, gz) - t from the device clock
                new_rows.extend((r[0] / 1e6, *to_engineering(r)) for r in rows)
            elif kind == "connected":
                self.ble.connected = True
                self.connecting = False
                self._sample_count = 0
                self._rate_hist.clear()
                self._say(f"connected {payload}")
                # the device streams on subscribe; just sync the rate
                self.ble.command(f"S,{self.rate_hz}")
            elif kind == "disconnected":
                self.ble.connected = False
                self.connecting = False
                self._rate_hist.clear()
                self._say("disconnected")
            elif kind in ("error", "log"):
                if kind == "error":
                    self.connecting = False
                self._say(f"ERROR: {payload}" if kind == "error" else str(payload))

        if new_rows:
            self._sample_count += len(new_rows)
            now = time.monotonic()
            self._rate_hist.append((now, self._sample_count))
            self._rate_hist = [(t, n) for t, n in self._rate_hist
                               if now - t <= RATE_WINDOW_S]
        elif not self.ble.connected:
            self._rate_hist.clear()

        return new_rows

    # -- read-only views --------------------------------------------------
    def stats(self) -> dict:
        r = self.rec
        hz = r.samples / r.duration if r.active and r.duration > 0 else 0.0

        # sliding-window rate: tracks reality instead of drifting towards a
        # lifetime average (a long stall at the start would dilute it forever)
        rate_inst = 0.0
        if len(self._rate_hist) >= 2:
            (t0, n0), (t1, n1) = self._rate_hist[0], self._rate_hist[-1]
            span = t1 - t0
            if span > 0.3:
                rate_inst = (n1 - n0) / span

        return {
            "connected": self.ble.connected,
            "connecting": self.connecting,
            "address": self.ble.address or "-",
            "recording": r.active,
            "rate_hz": self.rate_hz,
            "samples": r.samples,
            "live_samples": self._sample_count,
            "measured_hz": hz,
            "rate_inst": rate_inst,
            "dropped": r.dropped,
            "gaps": r.seq_gaps,
            "marks": len(r.marks),
            "session": r.dir.name if r.dir else "-",
            "duration": r.duration if r.active else 0.0,
        }

    def drain_log(self) -> list[str]:
        out, self.log = self.log, []
        return out

    def _say(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {msg}")
