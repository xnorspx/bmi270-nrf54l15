"""KneeChe IMU Collector - NiceGUI front end.

    python tools/app.py            native window (WebView2)
    python tools/app.py --web      serve on http://127.0.0.1:8080 for a browser

The data path lives in collector/session.py and knows nothing about the UI, so
it can be driven headless by the end-to-end test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import plotly.graph_objects as go
from nicegui import app, ui
from plotly.subplots import make_subplots

from collector import DEVICE_NAME, RATES, CollectorSession

CAPACITY = 1500          # points kept per trace (plotly trims to this)
UI_TICK = 0.05           # 20 Hz UI refresh; the 400 Hz data stays in the queue
STATS_TICK = 0.25

ACCENT = "#38bdf8"
C_OK = "#22c55e"
C_REC = "#ef4444"
C_WARN = "#f59e0b"

TRACE_COLOURS = ("#38bdf8", "#22c55e", "#f59e0b")

CSS = """
<style>
  body { background: #0b0f16; }
  .q-card { background: #121826 !important; border: 1px solid #1e2637; }
  .panel-title { font-size: 11px; letter-spacing: .12em; color: #7c8798;
                 text-transform: uppercase; }
  .stat-row { font-family: ui-monospace, Consolas, monospace; font-size: 12px;
              color: #aeb9c9; display: flex; justify-content: space-between; }
  .stat-val { color: #e6edf3; }
</style>
"""


def build_figure(demo: bool = False) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.12)
    for i, name in enumerate(("ax", "ay", "az")):
        fig.add_trace(go.Scattergl(x=[], y=[], name=name, mode="lines",
                                   line=dict(width=1, color=TRACE_COLOURS[i])),
                      row=1, col=1)
    for i, name in enumerate(("gx", "gy", "gz")):
        fig.add_trace(go.Scattergl(x=[], y=[], name=name, mode="lines",
                                   line=dict(width=1, color=TRACE_COLOURS[i])),
                      row=2, col=1)

    if demo:
        import math
        xs = [i * 0.005 for i in range(600)]
        for i in range(3):
            fig.data[i].x = xs
            fig.data[i].y = [math.sin(2 * math.pi * (0.8 + i * 0.35) * t + i) * 0.35
                             for t in xs]
        for i in range(3, 6):
            fig.data[i].x = xs
            fig.data[i].y = [math.sin(2 * math.pi * (1.5 + (i - 3) * 0.6) * t) * 22
                             for t in xs]

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#121826", plot_bgcolor="#0f131a",
        margin=dict(l=58, r=12, t=34, b=30),
        height=430,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                    bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        font=dict(family="Segoe UI, system-ui", size=11, color="#8b949e"),
        uirevision="keep",
    )
    fig.update_xaxes(gridcolor="#1e2637", zeroline=False)
    fig.update_yaxes(gridcolor="#1e2637", zeroline=False)
    fig.update_yaxes(title_text="g", row=1, col=1,
                     title_font=dict(size=10, color="#7c8798"))
    fig.update_yaxes(title_text="deg/s", row=2, col=1,
                     title_font=dict(size=10, color="#7c8798"))
    return fig


class CollectorUI:
    def __init__(self, demo: bool = False) -> None:
        self.session = CollectorSession(Path.cwd() / "sessions")
        self.session.start()
        self.pending: list[tuple] = []
        self.typing = False
        self.sel_index: int | None = None
        self.demo = demo
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        ui.dark_mode(True)
        ui.colors(primary=ACCENT)
        ui.add_head_html(CSS)

        with ui.header().classes("items-center justify-between px-4 py-2").style(
                "background:#0b0f16; border-bottom:1px solid #1e2637"):
            with ui.row().classes("items-center gap-3"):
                ui.icon("sensors", size="sm").style(f"color:{ACCENT}")
                ui.label("KneeChe IMU Collector").classes("text-lg font-medium")
                ui.label(DEVICE_NAME).classes("text-xs").style("color:#7c8798")
            with ui.row().classes("items-center gap-5"):
                self.stat_labels: dict[str, ui.label] = {}
                for key, caption in (("samples", "samples"), ("rate", "Hz"),
                                     ("dropped", "dropped"), ("gaps", "gaps"),
                                     ("marks", "marks")):
                    with ui.row().classes("items-baseline gap-1"):
                        self.stat_labels[key] = ui.label("-").classes("text-sm").style(
                            "color:#e6edf3; font-family:Consolas,monospace")
                        ui.label(caption).classes("text-xs").style("color:#7c8798")
                self.lbl_conn = ui.label("disconnected").classes("text-xs").style(
                    "color:#7c8798")
                self.lbl_rec = ui.label("idle").classes("text-xs").style(
                    f"color:{C_WARN}")

        with ui.row().classes("w-full no-wrap gap-3 p-3 items-start"):
            with ui.column().classes("gap-3").style("width:312px; flex:none"):
                self._device_card()
                self._record_card()
                self._marks_card()
            with ui.column().classes("gap-3 grow min-w-0"):
                with ui.card().classes("w-full"):
                    self.chart = ui.plotly(build_figure(self.demo)).classes("w-full")
                with ui.card().classes("w-full"):
                    ui.label("LOG").classes("panel-title")
                    self.log = ui.log(max_lines=200).classes("w-full").style(
                        "height:170px; font-family:ui-monospace,Consolas,monospace;"
                        "font-size:11px")

        ui.keyboard(on_key=self._on_key)
        ui.timer(UI_TICK, self._pump)
        ui.timer(STATS_TICK, self._refresh_stats)

        self._say("ready. Connect to the IMU to start the live stream.")
        self._refresh_stats()
        self._refresh_marks()

    def _device_card(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("DEVICE").classes("panel-title")
            with ui.row().classes("w-full gap-2 items-center no-wrap"):
                self.btn_conn = ui.button("Connect", on_click=self._toggle_connect)
                self.btn_conn.props("unelevated no-caps").classes("grow")
            with ui.row().classes("w-full gap-2 items-center no-wrap"):
                self.sel_rate = ui.select(RATES, value=100, label="sample rate (Hz)") \
                    .classes("grow").props("dense outlined")
                ui.button("Apply", on_click=self._apply_rate).props("flat dense no-caps")

    def _record_card(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("RECORDING").classes("panel-title")
            self.btn_rec = ui.button("Start recording", icon="radio_button_checked",
                                     on_click=self._toggle_record).props(
                "unelevated no-caps").classes("w-full")
            self.btn_rec.style(f"background:{C_OK}; color:#06210f")
            self.lbl_session = ui.label("-").classes("text-xs").style(
                "color:#7c8798; font-family:Consolas,monospace")

    def _marks_card(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("MARK POINT").classes("panel-title")
            self.in_label = ui.input("label", value="event").props("dense outlined") \
                .classes("w-full") \
                .on("focus", lambda: setattr(self, "typing", True)) \
                .on("blur", lambda: setattr(self, "typing", False))
            self.in_note = ui.input("note (可留空，之後再補)").props("dense outlined") \
                .classes("w-full") \
                .on("focus", lambda: setattr(self, "typing", True)) \
                .on("blur", lambda: setattr(self, "typing", False))
            ui.button("Mark point  (space)", icon="add_location_alt",
                      on_click=self._mark).props("unelevated no-caps").classes("w-full") \
                .style(f"background:{C_WARN}; color:#221805")

            ui.separator().style("border-color:#1e2637")
            with ui.column().classes("w-full gap-2") as self.marks_box:
                self.tbl = ui.table(
                    columns=[{"name": "no", "label": "#", "field": "no", "align": "left"},
                             {"name": "label", "label": "label", "field": "label",
                              "align": "left"},
                             {"name": "note", "label": "note", "field": "note",
                              "align": "left"}],
                    rows=[], row_key="no", selection="single",
                    on_select=self._on_select,
                ).classes("w-full").props(
                    'dense flat dark separator=none wrap-cells') \
                    .style("max-height:170px; font-size:12px")
                with ui.row().classes("w-full gap-2 no-wrap"):
                    ui.button("Edit", icon="edit", on_click=self._edit_mark).props(
                        "flat dense no-caps").classes("grow")
                    ui.button("Delete", icon="delete", on_click=self._delete_mark).props(
                        "flat dense no-caps color=negative").classes("grow")

    # ------------------------------------------------------------- actions
    def _toggle_connect(self) -> None:
        if self.session.ble.connected:
            self.session.disconnect()
        else:
            self.btn_conn.props("loading")
            self.session.connect()

    def _apply_rate(self) -> None:
        try:
            self.session.set_rate(int(self.sel_rate.value))
        except Exception as exc:                       # noqa: BLE001
            self._say(f"ERROR: {exc}")

    def _toggle_record(self) -> None:
        try:
            if self.session.rec.active:
                d = self.session.stop_recording()
                self.btn_rec.text = "Start recording"
                self.btn_rec.style(f"background:{C_OK}; color:#06210f")
                self.lbl_session.text = (f"saved {d.name}" if d else "-")
            else:
                self.session.start_recording()
                self.btn_rec.text = "Stop recording"
                self.btn_rec.style(f"background:{C_REC}; color:#2a0606")
                self.lbl_session.text = self.session.rec.dir.name
        except Exception as exc:                       # noqa: BLE001
            self._say(f"ERROR: {exc}")
            ui.notify(str(exc), type="negative")

    def _mark(self) -> None:
        self.session.add_mark(self.in_label.value or "event",
                              (self.in_note.value or "").strip())
        self.in_note.value = ""
        self._refresh_marks()

    def _on_select(self, event) -> None:
        sel = event.selection
        self.sel_index = int(sel[0]["no"]) - 1 if sel else None

    def _edit_mark(self) -> None:
        if self.sel_index is None:
            ui.notify("select a row first", type="info")
            return
        marks = self.session.rec.marks
        if not 0 <= self.sel_index < len(marks):
            return
        m = marks[self.sel_index]
        with ui.dialog() as dlg, ui.card().classes("w-96"):
            ui.label(f"mark #{self.sel_index + 1}  ·  sample {m['sample_index']}") \
                .classes("text-xs").style("color:#7c8798")
            e_label = ui.input("label", value=m["label"]).props("dense outlined") \
                .classes("w-full")
            e_note = ui.input("note", value=m["note"]).props("dense outlined") \
                .classes("w-full")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dlg.close).props("flat")

                def save() -> None:
                    self.session.update_mark(self.sel_index, e_label.value or "event",
                                             (e_note.value or "").strip())
                    self._refresh_marks()
                    dlg.close()

                ui.button("Save", on_click=save).props("unelevated")
        dlg.open()
        e_note.run_method("focus")

    def _delete_mark(self) -> None:
        if self.sel_index is None:
            ui.notify("select a row first", type="info")
            return
        self.session.delete_mark(self.sel_index)
        self.sel_index = None
        self._refresh_marks()

    def _on_key(self, e) -> None:
        if self.typing:
            return
        if e.action.keydown and e.key.name == "space":
            self._mark()
        elif e.action.keydown and e.key.name in ("delete", "backspace"):
            self._delete_mark()

    # --------------------------------------------------------------- timer
    def _pump(self) -> None:
        rows = self.session.pump()
        if rows:
            self.pending.extend(rows)
        self._push_log()
        if self.pending:
            self._flush_plot()

    def _flush_plot(self) -> None:
        batch, self.pending = self.pending, []
        xs = [r[0] for r in batch]
        cols = list(zip(*(r[1:] for r in batch)))
        update = {"x": [xs] * 6, "y": [list(c) for c in cols]}
        try:
            self.chart.run_plot_method("extendTraces", update, list(range(6)), CAPACITY)
        except Exception as exc:                       # noqa: BLE001
            self._say(f"plot update failed: {exc}")

    def _push_log(self) -> None:
        for line in self.session.drain_log():
            self.log.push(line)

    def _refresh_stats(self) -> None:
        st = self.session.stats()
        self.stat_labels["samples"].text = f"{st['live_samples']:,}"

        # the instantaneous rate, not a lifetime average: show it, and tint it
        # when it drifts away from the configured rate (real drops are visible).
        # It is fed whenever samples arrive, so previewing shows a rate too.
        if st["rate_inst"] > 0:
            self.stat_labels["rate"].text = f"{st['rate_inst']:.1f}"
            target = max(1, st["rate_hz"])
            off = abs(st["rate_inst"] - target) > 0.1 * target
            self.stat_labels["rate"].style(
                f"color:{C_WARN if off else C_OK}; font-family:Consolas,monospace")
        else:
            self.stat_labels["rate"].text = "—"
            self.stat_labels["rate"].style(
                "color:#e6edf3; font-family:Consolas,monospace")
        self.stat_labels["dropped"].text = str(st["dropped"])
        self.stat_labels["gaps"].text = str(st["gaps"])
        self.stat_labels["marks"].text = str(st["marks"])

        # state comes from stats(), never from parsing the human-readable log
        if st["connected"]:
            self.lbl_conn.text = f"● connected · {st['address']}"
            self.lbl_conn.style(f"color:{C_OK}")
        else:
            self.lbl_conn.text = "disconnected"
            self.lbl_conn.style("color:#7c8798")

        if st["recording"]:
            self.lbl_rec.text = "● REC"
            self.lbl_rec.style(f"color:{C_REC}")
            self.lbl_session.text = f"{st['session']} · {st['samples']:,} recorded"
        elif st["connected"]:
            self.lbl_rec.text = "idle"
            self.lbl_rec.style(f"color:{C_WARN}")
        else:
            self.lbl_rec.text = ""
            self.lbl_rec.style("color:#7c8798")

        if not st["connecting"]:
            self.btn_conn.props(remove="loading")
        self.btn_conn.text = "Disconnect" if st["connected"] else "Connect"

    def _refresh_marks(self) -> None:
        rows = [{"no": i + 1, "label": m["label"],
                 "note": f"{m['t_dev_us'] / 1e6:.2f}s  {m['note']}"}
                for i, m in enumerate(self.session.rec.marks)]
        self.tbl.rows = rows
        self.tbl.update()
        # an empty Quasar table renders a warning icon; hide the group instead
        self.marks_box.set_visibility(bool(rows))
        self.sel_index = None

    def _say(self, msg: str) -> None:
        self.log.push(msg)


def pick_port(preferred: int = 8080) -> int:
    """Return a free port. A stale instance still holding the port would serve
    the OLD page and make every fix look like it did not apply."""
    import socket

    for candidate in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    return preferred


def main() -> None:
    collector = CollectorUI(demo="--demo" in sys.argv)

    # --auto drives the panel by itself: connect, record, mark. Handy for a kiosk
    # demo and for verifying the live plot path without a human.
    if "--auto" in sys.argv:
        ui.timer(1.2, collector._toggle_connect, once=True)
        ui.timer(5.0, collector._toggle_record, once=True)
        ui.timer(8.0, collector._mark, once=True)
        ui.timer(10.0, lambda: collector.in_label.set_value("heel_strike"), once=True)
        ui.timer(12.5, collector._mark, once=True)

    port = pick_port(8080)
    if port != 8080:
        print(f"# port 8080 is busy (another instance?) - using {port}")

    native = "--web" not in sys.argv
    ui.run(
        native=native,
        title="KneeChe IMU Collector",
        window_size=(1280, 820) if native else None,
        reload=False,
        show=False,
        port=port,
        favicon="📈",
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
