"""KneeChe IMU collector - BLE acquisition panel for ML dataset recording.

Layout
    left  : device / rate / recording controls, mark point + note, live stats
    right : rolling accel + gyro plot

Output (one directory per recording session)

    session_YYYYmmdd_HHMMSS/
        imu.csv     t_dev_us,t_host_ns,ax_mg,ay_mg,az_mg,gx_mdps,gy_mdps,gz_mdps
        marks.jsonl one JSON object per mark point
        meta.json   device, rate, counts, gaps, timing

Run with:  python tools/collector_ui.py
"""

from __future__ import annotations

import asyncio
import json
import queue
import statistics
import struct
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from bleak import BleakClient, BleakScanner
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# ----------------------------------------------------------------- protocol

SVC_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
DEVICE_NAME = "KneeChe IMU"

FRAME_SAMPLES = 0x01
ACC_LSB_PER_G = 4096.0
GYR_LSB_PER_DPS = 65.536

RATES = [25, 50, 100, 200, 400]

BG = "#12161f"
BG_PANEL = "#1a202c"
FG = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#38bdf8"
OK = "#22c55e"
WARN = "#f59e0b"


def decode(payload: bytes) -> list[tuple] | None:
    if len(payload) < 6 or payload[0] != FRAME_SAMPLES:
        return None
    _seq, _dropped, n = struct.unpack_from("<HHB", payload, 1)
    if len(payload) < 6 + n * 16:
        return None
    out = []
    for i in range(n):
        out.append(struct.unpack_from("<Ihhhhhh", payload, 6 + i * 16))
    return out


# ---------------------------------------------------------------- recorder


class Recorder:
    """Writes one session directory. Thread-safe: fed from the BLE thread."""

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

    def start(self, root: Path, rate_hz: int, device: str) -> Path:
        with self._lock:
            stamp = datetime.now().strftime("session_%Y%m%d_%H%M%S")
            self.dir = root / stamp
            self.dir.mkdir(parents=True, exist_ok=True)
            self._csv = open(self.dir / "imu.csv", "w", encoding="utf-8", newline="")
            self._csv.write("t_dev_us,t_host_ns,ax_mg,ay_mg,az_mg,gx_mdps,gy_mdps,gz_mdps\n")
            self.marks = []
            self.rate_hz = rate_hz
            self.samples = 0
            self.dropped = 0
            self.seq_gaps = 0
            self._last_seq = None
            self._last_dev_us = 0
            self.started_at = time.monotonic()
            self._device = device
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
            self._last_dev_us = rows[-1][0] if rows else self._last_dev_us
            self.samples += len(rows)

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
        """Marks stay editable for the rest of the session, including after the
        recording was stopped - the jsonl file is rewritten from memory."""
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

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    def stop(self) -> Path | None:
        with self._lock:
            if not self.active:
                return None
            d = self.dir
            try:
                self._csv.flush()
                self._csv.close()
            except Exception:
                pass
            self._csv = None
            self._flush_marks()
            meta = {
                "device": self._device,
                "device_name": DEVICE_NAME,
                "rate_hz": self.rate_hz,
                "samples": self.samples,
                "dropped_device": self.dropped,
                "seq_gaps": self.seq_gaps,
                "duration_s": round(self.duration, 3),
                "units": {"accel": "mg", "gyro": "mdps", "t_dev_us": "device uptime us"},
                "accel_range_g": 8,
                "gyro_range_dps": 500,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
            (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return d


# --------------------------------------------------------------- ble thread


class BleSource:
    """Owns the asyncio loop in a background thread; posts events to a Queue."""

    def __init__(self) -> None:
        self.events: queue.Queue = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: BleakClient | None = None
        self._ready = threading.Event()
        self.connected = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # submit() is useless before the loop exists, so block until it does
        self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def submit(self, coro) -> None:
        if self._loop is None:
            self.events.put(("error", "BLE loop not running"))
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # -- actions ---------------------------------------------------------
    def connect(self) -> None:
        self.submit(self._connect())

    async def _connect(self) -> None:
        try:
            self.events.put(("log", f"scanning for {DEVICE_NAME} ..."))
            dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=12.0)
            if dev is None:
                self.events.put(("error", "device not found - is it powered and advertising?"))
                return
            self._client = BleakClient(dev, timeout=20.0)
            await self._client.connect()
            await self._client.start_notify(TX_UUID, self._on_notify)
            self.connected = True
            self.events.put(("connected", dev.address))
        except Exception as exc:                      # noqa: BLE001
            self.events.put(("error", f"connect failed: {exc}"))

    def disconnect(self) -> None:
        self.submit(self._disconnect())

    async def _disconnect(self) -> None:
        try:
            if self._client:
                if self._client.is_connected:
                    await self._client.stop_notify(TX_UUID)
                    await self._client.disconnect()
        except Exception:
            pass
        self.connected = False
        self.events.put(("disconnected", ""))

    def command(self, text: str) -> None:
        async def _send() -> None:
            try:
                if self._client and self._client.is_connected:
                    await self._client.write_gatt_char(RX_UUID, (text + "\n").encode(),
                                                       response=False)
            except Exception as exc:                  # noqa: BLE001
                self.events.put(("error", f"command failed: {exc}"))
        self.submit(_send())

    def _on_notify(self, _char, data: bytearray) -> None:
        payload = bytes(data)
        seq, dropped, _n = struct.unpack_from("<HHB", payload, 1)
        rows = decode(payload)
        if rows:
            self.events.put(("samples", (seq, dropped, rows)))


# ---------------------------------------------------------------------- UI


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.ble = BleSource()
        self.rec = Recorder()
        self.session_root = Path.cwd() / "sessions"
        self.session_root.mkdir(exist_ok=True)
        self.plot_t: list[float] = []
        self.plot_a: list[tuple] = []
        self.plot_g: list[tuple] = []
        self.t0 = time.monotonic()
        self._build()
        self.ble.start()
        self.root.after(75, self._pump)

    # -- construction ----------------------------------------------------
    def _build(self) -> None:
        self.root.title("KneeChe IMU Collector")
        self.root.configure(bg=BG)
        self.root.geometry("1180x680")

        left = tk.Frame(self.root, bg=BG_PANEL, width=300)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(10, 6), pady=10)
        left.pack_propagate(False)

        tk.Label(left, text="DEVICE", bg=BG_PANEL, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(14, 2))
        self.btn_conn = tk.Button(left, text="Connect", command=self._toggle_connect,
                                  bg=ACCENT, fg="#04121f", relief="flat",
                                  font=("Segoe UI", 10, "bold"), height=2)
        self.btn_conn.pack(fill=tk.X, padx=14)
        self.lbl_state = tk.Label(left, text="disconnected", bg=BG_PANEL, fg=MUTED,
                                  font=("Segoe UI", 9))
        self.lbl_state.pack(anchor="w", padx=14, pady=(6, 12))

        tk.Label(left, text="SAMPLE RATE", bg=BG_PANEL, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(4, 2))
        self.var_rate = tk.StringVar(value="100")
        self.cmb_rate = ttk.Combobox(left, textvariable=self.var_rate, state="readonly",
                                     values=[str(r) for r in RATES])
        self.cmb_rate.pack(fill=tk.X, padx=14)
        self.cmb_rate.bind("<<ComboboxSelected>>", lambda _e: self._apply_rate())
        self.btn_rate = tk.Button(left, text="Apply rate", command=self._apply_rate,
                                  bg=BG_PANEL, fg=FG, relief="flat",
                                  highlightbackground=MUTED)
        self.btn_rate.pack(fill=tk.X, padx=14, pady=(4, 12))

        tk.Label(left, text="RECORDING", bg=BG_PANEL, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(4, 2))
        self.btn_rec = tk.Button(left, text="● Start recording", command=self._toggle_record,
                                 bg=OK, fg="#04121f", relief="flat",
                                 font=("Segoe UI", 10, "bold"), height=2)
        self.btn_rec.pack(fill=tk.X, padx=14)
        self.lbl_rec = tk.Label(left, text="idle", bg=BG_PANEL, fg=MUTED,
                                font=("Segoe UI", 9))
        self.lbl_rec.pack(anchor="w", padx=14, pady=(6, 12))

        tk.Label(left, text="MARK POINT", bg=BG_PANEL, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(4, 2))
        tk.Label(left, text="label", bg=BG_PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=14)
        self.ent_label = tk.Entry(left, bg="#0f131a", fg=FG, insertbackground=FG,
                                  relief="flat")
        self.ent_label.pack(fill=tk.X, padx=14, pady=(1, 6))
        self.ent_label.insert(0, "event")
        tk.Label(left, text="note", bg=BG_PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=14)
        self.ent_note = tk.Entry(left, bg="#0f131a", fg=FG, insertbackground=FG,
                                 relief="flat")
        self.ent_note.pack(fill=tk.X, padx=14, pady=(1, 6))
        self.btn_mark = tk.Button(left, text="✚ Mark point  (space)", command=self._mark,
                                  bg=WARN, fg="#1a1205", relief="flat",
                                  font=("Segoe UI", 10, "bold"), height=2)
        self.btn_mark.pack(fill=tk.X, padx=14, pady=(2, 10))

        tk.Label(left, text="MARKS  (double-click to edit, Del removes)", bg=BG_PANEL,
                 fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(0, 2))
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Marks.Treeview", background="#0f131a", fieldbackground="#0f131a",
                        foreground=FG, rowheight=19, borderwidth=0)
        style.configure("Marks.Treeview.Heading", background=BG_PANEL, foreground=MUTED,
                        borderwidth=0, font=("Segoe UI", 8))
        style.map("Marks.Treeview", background=[("selected", "#1e3a52")])
        self.tree = ttk.Treeview(left, columns=("no", "label", "note"), show="headings",
                                 height=7, style="Marks.Treeview")
        for col, txt, width in (("no", "#", 26), ("label", "label", 86),
                                ("note", "note", 168)):
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=width, anchor="w", stretch=(col == "note"))
        self.tree.pack(fill=tk.X, padx=14, pady=(0, 8))
        self.tree.bind("<Double-1>", self._edit_mark)
        self.tree.bind("<Delete>", lambda _e: self._delete_mark())

        self.lbl_stats = tk.Label(left, text="", bg=BG_PANEL, fg=MUTED,
                                  justify="left", anchor="w",
                                  font=("Consolas", 9))
        self.lbl_stats.pack(fill=tk.X, padx=14, pady=(4, 14))

        right = tk.Frame(self.root, bg=BG)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10), pady=10)

        self.fig = Figure(figsize=(7, 3.4), dpi=100, facecolor=BG)
        self.ax_a = self.fig.add_subplot(211, facecolor="#0f131a")
        self.ax_g = self.fig.add_subplot(212, facecolor="#0f131a")
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.log = tk.Text(right, height=8, bg="#0f131a", fg=MUTED, relief="flat",
                           font=("Consolas", 9))
        self.log.pack(fill=tk.X, pady=(8, 0))
        self.log.insert("end", "ready.\n")

        self.root.bind("<space>", lambda _e: self._mark())
        self._refresh_stats()

    # -- helpers ---------------------------------------------------------
    def _say(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{stamp}] {msg}\n")
        self.log.see("end")

    def _toggle_connect(self) -> None:
        if self.ble.connected:
            self.ble.disconnect()
        else:
            self.btn_conn.config(text="Connecting ...", state=tk.DISABLED)
            self.ble.connect()

    def _apply_rate(self) -> None:
        hz = int(self.var_rate.get())
        self.ble.command(f"S,{hz}")
        self.rec.rate_hz = hz
        self._say(f"rate -> {hz} Hz")

    def _toggle_record(self) -> None:
        if self.rec.active:
            d = self.rec.stop()
            n_marks = len(self.rec.marks)
            self.btn_rec.config(text="● Start recording", bg=OK)
            self.lbl_rec.config(text=f"saved {self.rec.samples} samples, "
                                     f"{n_marks} marks (still editable)")
            if d:
                self._say(f"saved {d.name}  ({self.rec.samples} samples, {n_marks} marks)")
        else:
            if not self.ble.connected:
                messagebox.showwarning("Not connected", "Connect to the IMU first.")
                return
            hz = int(self.var_rate.get())
            d = self.rec.start(self.session_root, hz, DEVICE_NAME)
            self.ble.command(f"S,{hz}")
            self._refresh_marks()
            self.btn_rec.config(text="■ Stop recording", bg="#ef4444")
            self.lbl_rec.config(text=d.name)
            self._say(f"recording -> {d}  (the device was already streaming)")

    def _mark(self) -> None:
        if not self.rec.active:
            self._say("mark ignored - not recording")
            return
        label = self.ent_label.get().strip() or "event"
        note = self.ent_note.get().strip()
        self.rec.add_mark(label, note)
        self._refresh_marks()
        self._say(f"mark #{len(self.rec.marks)}  [{label}]  {note or '(no note)'}")
        self.ent_note.delete(0, "end")

    def _refresh_marks(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, m in enumerate(self.rec.marks):
            self.tree.insert("", "end",
                             values=(i + 1, m["label"],
                                     f"{m['t_dev_us'] / 1e6:.3f}s  {m['note']}"))

    def _edit_mark(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if not 0 <= idx < len(self.rec.marks):
            return
        mark = self.rec.marks[idx]

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Edit mark #{idx + 1}")
        dlg.configure(bg=BG_PANEL)
        dlg.geometry("380x180")
        dlg.transient(self.root)

        tk.Label(dlg, text=f"sample index {mark['sample_index']}   "
                           f"t_dev {mark['t_dev_us'] / 1e6:.3f}s",
                 bg=BG_PANEL, fg=MUTED, font=("Consolas", 8)).pack(anchor="w",
                                                                  padx=14, pady=(12, 6))
        tk.Label(dlg, text="label", bg=BG_PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=14)
        e_label = tk.Entry(dlg, bg="#0f131a", fg=FG, insertbackground=FG, relief="flat")
        e_label.pack(fill=tk.X, padx=14)
        e_label.insert(0, mark["label"])

        tk.Label(dlg, text="note", bg=BG_PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=14, pady=(8, 0))
        e_note = tk.Entry(dlg, bg="#0f131a", fg=FG, insertbackground=FG, relief="flat")
        e_note.pack(fill=tk.X, padx=14)
        e_note.insert(0, mark["note"])
        e_note.focus_set()

        def save() -> None:
            self.rec.update_mark(idx, e_label.get().strip() or "event",
                                 e_note.get().strip())
            self._refresh_marks()
            self._say(f"mark #{idx + 1} updated")
            dlg.destroy()

        row = tk.Frame(dlg, bg=BG_PANEL)
        row.pack(fill=tk.X, padx=14, pady=12)
        tk.Button(row, text="Save", command=save, bg=OK, fg="#04121f",
                  relief="flat").pack(side=tk.RIGHT)
        tk.Button(row, text="Cancel", command=dlg.destroy, bg=BG_PANEL, fg=FG,
                  relief="flat").pack(side=tk.RIGHT, padx=8)
        dlg.bind("<Return>", lambda _e: save())

    def _delete_mark(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        self.rec.delete_mark(idx)
        self._refresh_marks()
        self._say(f"mark #{idx + 1} deleted")

    def _refresh_stats(self) -> None:
        r = self.rec
        if r.active:
            hz = r.samples / r.duration if r.duration > 0 else 0
            self.lbl_stats.config(text=(
                f"samples {r.samples:>8}\n"
                f"rate    {hz:>8.1f} Hz\n"
                f"dropped {r.dropped:>8}\n"
                f"gaps    {r.seq_gaps:>8}\n"
                f"marks   {len(r.marks):>8}"
            ))
        else:
            self.lbl_stats.config(text=(
                "samples        -\n"
                "rate           Hz\n"
                "dropped        -\n"
                "gaps           -\n"
                "marks          -"
            ))
        self.root.after(250, self._refresh_stats)

    # -- data pump -------------------------------------------------------
    def _pump(self) -> None:
        updated = False
        try:
            for _ in range(400):
                kind, payload = self.ble.events.get_nowait()
                if kind == "samples":
                    seq, dropped, rows = payload
                    if self.rec.active:
                        self.rec.add_frame(seq, dropped, rows)
                    now = time.monotonic() - self.t0
                    for _t, ax, ay, az, gx, gy, gz in rows:
                        self.plot_t.append(now)
                        self.plot_a.append((ax / ACC_LSB_PER_G,
                                            ay / ACC_LSB_PER_G,
                                            az / ACC_LSB_PER_G))
                        self.plot_g.append((gx / GYR_LSB_PER_DPS,
                                            gy / GYR_LSB_PER_DPS,
                                            gz / GYR_LSB_PER_DPS))
                    updated = True
                elif kind == "connected":
                    self.ble.connected = True
                    self.btn_conn.config(text="Disconnect", state=tk.NORMAL)
                    self.lbl_state.config(text=f"connected  {payload}", fg=OK)
                    self._say(f"connected {payload} - streaming, not recording")
                    # the device streams on subscribe; just sync the rate
                    self.ble.command(f"S,{self.var_rate.get()}")
                elif kind == "disconnected":
                    self.ble.connected = False
                    self.btn_conn.config(text="Connect", state=tk.NORMAL)
                    self.lbl_state.config(text="disconnected", fg=MUTED)
                    self._say("disconnected")
                elif kind == "error":
                    self.btn_conn.config(text="Connect", state=tk.NORMAL)
                    self._say(f"ERROR: {payload}")
                elif kind == "log":
                    self._say(payload)
        except queue.Empty:
            pass

        if updated and len(self.plot_t) > 2:
            self._draw()
            self.plot_t = self.plot_t[-1500:]
            self.plot_a = self.plot_a[-1500:]
            self.plot_g = self.plot_g[-1500:]

        self.root.after(75, self._pump)

    def _draw(self) -> None:
        ax1, ax2 = self.ax_a, self.ax_g
        ax1.clear()
        ax2.clear()
        t = self.plot_t
        for i, (name, colour) in enumerate((("ax", "#38bdf8"), ("ay", "#22c55e"),
                                            ("az", "#f59e0b"))):
            ax1.plot(t[-len(self.plot_a):], [v[i] for v in self.plot_a],
                     color=colour, linewidth=1.0, label=name)
        for i, (name, colour) in enumerate((("gx", "#38bdf8"), ("gy", "#22c55e"),
                                            ("gz", "#f59e0b"))):
            ax2.plot(t[-len(self.plot_g):], [v[i] for v in self.plot_g],
                     color=colour, linewidth=1.0, label=name)
        ax1.set_ylabel("g", color=MUTED, fontsize=8)
        ax2.set_ylabel("deg/s", color=MUTED, fontsize=8)
        for ax in (ax1, ax2):
            ax.tick_params(colors=MUTED, labelsize=7)
            ax.grid(True, color="#1f2937", linewidth=0.5)
            for spine in ax.spines.values():
                spine.set_color("#1f2937")
            ax.legend(loc="upper right", fontsize=7, ncol=3, framealpha=0.0,
                      labelcolor=MUTED)
        self.fig.tight_layout()
        self.canvas.draw_idle()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
