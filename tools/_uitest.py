"""Construct the GUI without a human, to catch widget/config errors early."""

from __future__ import annotations

import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector_ui as cu  # noqa: E402


def main() -> int:
    root = tk.Tk()
    try:
        app = cu.App(root)
        # let the after() chain run a few times
        for _ in range(6):
            root.update()
            time.sleep(0.1)
        app._say("self-test message")
        app._mark()                       # not recording -> should log, not crash
        app.var_rate.set("200")
        app._refresh_stats()
        root.update()
        print("rate control:", app.var_rate.get())
        print("stats text  :", repr(app.lbl_stats.cget("text")[:40]))
        print("UI construction OK")
        return 0
    finally:
        root.destroy()


if __name__ == "__main__":
    sys.exit(main())
