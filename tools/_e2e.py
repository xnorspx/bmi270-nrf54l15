"""Drive the whole collector pipeline without a human: connect, record, mark, stop."""

from __future__ import annotations

import json
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector_ui as cu  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def pump(root: tk.Tk, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.03)


def main() -> int:
    root = tk.Tk()
    app = cu.App(root)
    try:
        print("connecting ...")
        app.ble.connect()
        deadline = time.time() + 20
        while time.time() < deadline and not app.ble.connected:
            root.update()
            time.sleep(0.05)
        if not app.ble.connected:
            print("FAIL: could not connect")
            return 1
        print("connected")

        app.var_rate.set("200")
        app._apply_rate()

        # preview: the device streams as soon as we subscribe, before recording
        pump(root, 1.5)
        preview = len(app.plot_t)
        print(f"preview samples before recording: {preview}")
        if preview < 50:
            print("FAIL: no live stream before recording")
            return 1

        print("starting recording ...")
        app._toggle_record()
        pump(root, 2.5)

        app.ent_label.delete(0, "end")
        app.ent_label.insert(0, "heel_strike")
        app.ent_note.delete(0, "end")
        app.ent_note.insert(0, "左腳落地 測試")
        app._mark()
        pump(root, 0.8)

        app._mark()
        pump(root, 0.4)

        print("stopping ...")
        app._toggle_record()
        pump(root, 0.4)

        # marks must stay editable after the recording is closed
        app.rec.update_mark(0, "heel_strike", "左腳落地（修正後備注）")
        app.rec.update_mark(1, "toe_off", "右腳離地")
        app._refresh_marks()
        root.update()

        sessions = sorted(app.session_root.glob("session_*"), key=lambda p: p.stat().st_mtime)
        if not sessions:
            print("FAIL: no session directory written")
            return 1
        d = sessions[-1]
        csv_lines = (d / "imu.csv").read_text(encoding="utf-8").strip().splitlines()
        marks = [json.loads(x) for x in
                 (d / "marks.jsonl").read_text(encoding="utf-8").strip().splitlines()]
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))

        print()
        print(f"session       : {d.name}")
        print(f"csv rows      : {len(csv_lines) - 1}")
        print(f"header        : {csv_lines[0]}")
        print(f"first sample  : {csv_lines[1] if len(csv_lines) > 1 else '-'}")
        print(f"marks         : {len(marks)}")
        for m in marks:
            print(f"   [{m['label']}] {m['note']}  @ sample {m['sample_index']}")
        print(f"meta          : rate={meta['rate_hz']} samples={meta['samples']} "
              f"dropped={meta['dropped_device']} gaps={meta['seq_gaps']}")

        ok = (len(csv_lines) > 200 and len(marks) == 2
              and meta["samples"] > 200 and meta["seq_gaps"] == 0)
        print()
        print("RESULT:", "end-to-end OK" if ok else "check the numbers above")
        return 0 if ok else 2
    finally:
        root.destroy()


if __name__ == "__main__":
    sys.exit(main())
