"""
NI cDAQ-9189 Thermocouple Data Acquisition GUI
Reads K-type thermocouples on all 16 channels of an NI-9213 module.

Features
--------
- Live rolling chart — toggle any channel on/off via sidebar checkboxes
- Select-All / Deselect-All buttons
- Record to CSV with timestamped filename (Record / Stop Recording buttons)
- Simulation mode when nidaqmx is not installed

Requirements
------------
    pip install nidaqmx matplotlib numpy

Hardware
--------
    NI cDAQ-9189 chassis (Ethernet)
    NI-9213 thermocouple module in slot 1
    K-type thermocouples on channels ai0 – ai15
"""

import csv
import collections
import os
import queue
import sys
import threading
import time
from datetime import datetime
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.animation as animation
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

try:
    import nidaqmx
    from nidaqmx.constants import (
        AcquisitionType,
        CJCSource,
        ThermocoupleType,
        TemperatureUnits,
    )
    NIDAQMX_AVAILABLE = True
except ImportError:
    NIDAQMX_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────────────────────────────────────
NUM_CHANNELS    = 16
HISTORY_SECONDS = 300        # max rolling buffer (seconds)
DEFAULT_DEVICE  = "cDAQ9189-24E8D67Mod1"
DEFAULT_RATE    = 2.0        # Hz

# Dark industrial palettec
BG_DARK  = "#0d1117"
BG_MED   = "#161b22"
BG_PANEL = "#21262d"
ACCENT   = "#58a6ff"
GREEN    = "#3fb950"
WARN     = "#f0883e"
RED      = "#f85149"
RECRED   = "#da3633"
TEXT_PRI = "#e6edf3"
TEXT_SEC = "#8b949e"
BORDER   = "#30363d"

# 16 perceptually-distinct colours
CHANNEL_COLORS = [
    "#58a6ff", "#3fb950", "#f0883e", "#bc8cff",
    "#ff7b72", "#79c0ff", "#56d364", "#ffa657",
    "#d2a8ff", "#ff8c94", "#a5f3d0", "#ffd700",
    "#00ced1", "#ff69b4", "#adff2f", "#ff8c00",
]


# ──────────────────────────────────────────────────────────────────────────────
#  DAQ worker
# ──────────────────────────────────────────────────────────────────────────────
class DAQWorker:
    """Acquires thermocouple data in a background thread."""

    def __init__(self, device: str, rate: float, out_queue: queue.Queue):
        self.device     = device
        self.rate       = rate
        self.out_queue  = out_queue
        self._stop      = threading.Event()
        self._thread    = None
        self.simulate   = not NIDAQMX_AVAILABLE

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)

    # ── hardware path ──────────────────────────────────────────────────────
    def _run_hardware(self):
        with nidaqmx.Task() as task:
            for ch in range(NUM_CHANNELS):
                task.ai_channels.add_ai_thrmcpl_chan(
                    f"{self.device}/ai{ch}",
                    thermocouple_type=ThermocoupleType.K,
                    units=TemperatureUnits.DEG_C,
                    cjc_source=CJCSource.BUILT_IN,
                )
            task.timing.cfg_samp_clk_timing(
                rate=self.rate,
                sample_mode=AcquisitionType.CONTINUOUS,
                samps_per_chan=10,
            )
            task.start()
            interval = 1.0 / self.rate
            while not self._stop.is_set():
                t0 = time.time()
                try:
                    raw   = task.read(number_of_samples_per_channel=1, timeout=2.0)
                    temps = [
                        ch_data[0] if isinstance(ch_data, list) else ch_data
                        for ch_data in raw
                    ]
                    self.out_queue.put((time.time(), temps))
                except Exception as exc:
                    self.out_queue.put(("error", str(exc)))
                    return
                time.sleep(max(0, interval - (time.time() - t0)))

    # ── simulation path ────────────────────────────────────────────────────
    def _run_simulation(self):
        bases    = [20.0 + i * 2.5 for i in range(NUM_CHANNELS)]
        interval = 1.0 / self.rate
        t0       = time.time()
        while not self._stop.is_set():
            t = time.time() - t0
            temps = [
                bases[i]
                + 6   * np.sin(0.04 * t + i * 0.55)
                + 2   * np.sin(0.18 * t + i * 1.05)
                + np.random.normal(0, 0.12)
                for i in range(NUM_CHANNELS)
            ]
            self.out_queue.put((time.time(), temps))
            time.sleep(interval)

    def _run(self):
        if self.simulate:
            self._run_simulation()
        else:
            try:
                self._run_hardware()
            except Exception as exc:
                self.out_queue.put(("error", str(exc)))


# ──────────────────────────────────────────────────────────────────────────────
#  CSV recorder
# ──────────────────────────────────────────────────────────────────────────────
class CSVRecorder:
    """Thread-safe CSV writer."""

    def __init__(self):
        self._file    = None
        self._writer  = None
        self._lock    = threading.Lock()
        self.filepath = ""
        self.row_count = 0

    def start(self, filepath: str):
        with self._lock:
            self._file = open(filepath, "w", newline="")
            self.filepath  = filepath
            self.row_count = 0
            header = ["Timestamp", "Elapsed_s"] + [f"Ch{i:02d}_degC" for i in range(NUM_CHANNELS)]
            self._writer = csv.writer(self._file)
            self._writer.writerow(header)
            self._file.flush()

    def write(self, timestamp: float, elapsed: float, temps: list):
        with self._lock:
            if self._writer is None:
                return
            ts_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            row    = [ts_str, f"{elapsed:.3f}"] + [f"{v:.4f}" for v in temps]
            self._writer.writerow(row)
            self.row_count += 1
            if self.row_count % 10 == 0:
                self._file.flush()

    def stop(self):
        with self._lock:
            if self._file:
                self._file.flush()
                self._file.close()
                self._file   = None
                self._writer = None

    @property
    def active(self) -> bool:
        return self._file is not None


# ──────────────────────────────────────────────────────────────────────────────
#  Main application
# ──────────────────────────────────────────────────────────────────────────────
class ThermocoupleApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("NI cDAQ-9189  ·  NI-9213  ·  16-Ch Thermocouple Monitor")
        self.configure(bg=BG_DARK)
        self.minsize(1160, 720)
        try:
            if sys.platform == "win32":
                self.state("zoomed")
            else:
                self.attributes("-zoomed", True)
        except Exception:
            pass

        # ── data state ─────────────────────────────────────────────────────
        self._running     = False
        self._data_queue  = queue.Queue(maxsize=2000)
        self._worker      = None
        self._ani         = None
        self._start_time  = None
        self._sample_total = 0

        maxlen = int(HISTORY_SECONDS * 200)
        self._times = collections.deque(maxlen=maxlen)
        self._temps = [collections.deque(maxlen=maxlen) for _ in range(NUM_CHANNELS)]

        # per-channel visibility
        self._ch_visible = [tk.BooleanVar(value=True) for _ in range(NUM_CHANNELS)]

        # CSV recorder
        self._recorder = CSVRecorder()

        # ── build UI ───────────────────────────────────────────────────────
        self._build_header()
        self._build_main()
        self._build_status_bar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─────────────────────────────────────────────────────────────────────────
    #  UI construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_header(self):
        hdr = tk.Frame(self, bg=BG_DARK, pady=9)
        hdr.pack(fill="x", padx=18)

        # left: title
        tk.Label(hdr, text="◉", fg=ACCENT, bg=BG_DARK,
                 font=("Consolas", 13)).pack(side="left", padx=(0, 8))
        tk.Label(hdr, text="cDAQ-9189  /  NI-9213  —  16-Channel Thermocouple Monitor",
                 fg=TEXT_PRI, bg=BG_DARK,
                 font=("Segoe UI", 15, "bold")).pack(side="left")

        # right: controls
        ctrl = tk.Frame(hdr, bg=BG_DARK)
        ctrl.pack(side="right")

        def lbl(text):
            tk.Label(ctrl, text=text, fg=TEXT_SEC, bg=BG_DARK,
                     font=("Segoe UI", 10)).pack(side="left", padx=(0, 4))

        def entry(var, w):
            e = tk.Entry(ctrl, textvariable=var, width=w,
                         bg=BG_PANEL, fg=TEXT_PRI, insertbackground=TEXT_PRI,
                         relief="flat", font=("Consolas", 10), bd=4)
            e.pack(side="left", padx=(0, 14))
            return e

        lbl("Device:")
        self._device_var = tk.StringVar(value=DEFAULT_DEVICE)
        entry(self._device_var, 14)

        lbl("Rate (Hz):")
        self._rate_var = tk.StringVar(value=str(DEFAULT_RATE))
        entry(self._rate_var, 7)

        self._start_btn = tk.Button(
            ctrl, text="▶  Start", command=self._toggle_acquisition,
            bg=GREEN, fg=BG_DARK, activebackground="#2ea043", activeforeground=BG_DARK,
            font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=5, cursor="hand2",
        )
        self._start_btn.pack(side="left", padx=(0, 6))

        tk.Button(
            ctrl, text="⟳  Clear", command=self._clear_data,
            bg=BG_PANEL, fg=TEXT_SEC, activebackground=BORDER, activeforeground=TEXT_PRI,
            font=("Segoe UI", 10), relief="flat", padx=10, pady=5, cursor="hand2",
        ).pack(side="left")

        ttk.Separator(self, orient="horizontal").pack(fill="x")

    # ── main paned layout ──────────────────────────────────────────────────
    def _build_main(self):
        pane = tk.PanedWindow(self, orient="horizontal",
                              bg=BG_DARK, sashwidth=5, sashrelief="flat",
                              sashpad=2)
        pane.pack(fill="both", expand=True)

        self._build_sidebar(pane)
        self._build_chart(pane)

    # ── sidebar ────────────────────────────────────────────────────────────
    def _build_sidebar(self, pane):
        sb = tk.Frame(pane, bg=BG_MED, width=230)
        pane.add(sb, minsize=210)

        # ── channel header + select all/none ──────────────────────────────
        ch_hdr = tk.Frame(sb, bg=BG_MED, padx=12, pady=8)
        ch_hdr.pack(fill="x")

        tk.Label(ch_hdr, text="CHANNELS", fg=TEXT_SEC, bg=BG_MED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left")

        btn_frm = tk.Frame(ch_hdr, bg=BG_MED)
        btn_frm.pack(side="right")

        def _small_btn(parent, text, cmd):
            return tk.Button(
                parent, text=text, command=cmd,
                bg=BG_PANEL, fg=TEXT_SEC, activebackground=BORDER,
                activeforeground=TEXT_PRI, font=("Segoe UI", 8),
                relief="flat", padx=5, pady=2, cursor="hand2",
            )

        _small_btn(btn_frm, "All",  self._select_all).pack(side="left", padx=(0, 3))
        _small_btn(btn_frm, "None", self._deselect_all).pack(side="left")

        ttk.Separator(sb, orient="horizontal").pack(fill="x")

        # scrollable channel list
        canvas_wrap = tk.Canvas(sb, bg=BG_MED, highlightthickness=0, bd=0)
        scrollbar   = ttk.Scrollbar(sb, orient="vertical", command=canvas_wrap.yview)
        canvas_wrap.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas_wrap.pack(side="left", fill="both", expand=True)

        ch_list = tk.Frame(canvas_wrap, bg=BG_MED)
        win_id  = canvas_wrap.create_window((0, 0), window=ch_list, anchor="nw")

        def _on_frame_configure(e):
            canvas_wrap.configure(scrollregion=canvas_wrap.bbox("all"))
        def _on_canvas_configure(e):
            canvas_wrap.itemconfig(win_id, width=e.width)

        ch_list.bind("<Configure>", _on_frame_configure)
        canvas_wrap.bind("<Configure>", _on_canvas_configure)

        # mouse-wheel scroll
        def _on_mousewheel(e):
            canvas_wrap.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas_wrap.bind_all("<MouseWheel>", _on_mousewheel)

        self._temp_labels = []
        for i in range(NUM_CHANNELS):
            row = tk.Frame(ch_list, bg=BG_MED, pady=2)
            row.pack(fill="x", padx=8)

            cb = tk.Checkbutton(
                row, variable=self._ch_visible[i],
                bg=BG_MED, activebackground=BG_MED, selectcolor=BG_PANEL,
                command=self._update_visibility,
            )
            cb.pack(side="left")

            tk.Label(row, text="●", fg=CHANNEL_COLORS[i], bg=BG_MED,
                     font=("Consolas", 11)).pack(side="left", padx=(0, 4))

            tk.Label(row, text=f"Ch {i:02d}", fg=TEXT_PRI, bg=BG_MED,
                     font=("Consolas", 10), width=6, anchor="w").pack(side="left")

            val = tk.Label(row, text="—", fg=CHANNEL_COLORS[i], bg=BG_MED,
                           font=("Consolas", 10, "bold"), anchor="e", width=9)
            val.pack(side="right")
            self._temp_labels.append(val)

        ttk.Separator(sb, orient="horizontal").pack(fill="x", pady=(6, 0))

        # ── statistics ─────────────────────────────────────────────────────
        stats_frm = tk.Frame(sb, bg=BG_MED, padx=14, pady=10)
        stats_frm.pack(fill="x")
        tk.Label(stats_frm, text="STATISTICS  (visible)", fg=TEXT_SEC, bg=BG_MED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, 4))
        self._stat_labels = {}
        for key, lbl in [("min", "Min"), ("max", "Max"), ("avg", "Mean"), ("spread", "Spread")]:
            r = tk.Frame(stats_frm, bg=BG_MED)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=f"{lbl}:", fg=TEXT_SEC, bg=BG_MED,
                     font=("Segoe UI", 9), width=7, anchor="w").pack(side="left")
            v = tk.Label(r, text="—", fg=TEXT_PRI, bg=BG_MED, font=("Consolas", 10))
            v.pack(side="right")
            self._stat_labels[key] = v

        ttk.Separator(sb, orient="horizontal").pack(fill="x", pady=(6, 0))

        # ── time window ────────────────────────────────────────────────────
        win_frm = tk.Frame(sb, bg=BG_MED, padx=14, pady=10)
        win_frm.pack(fill="x")
        tk.Label(win_frm, text="TIME WINDOW", fg=TEXT_SEC, bg=BG_MED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, 4))
        self._window_var = tk.IntVar(value=60)
        for s, lbl in [(30, "30 s"), (60, "1 min"), (120, "2 min"), (300, "5 min")]:
            tk.Radiobutton(
                win_frm, text=lbl, variable=self._window_var, value=s,
                bg=BG_MED, fg=TEXT_PRI, selectcolor=BG_PANEL,
                activebackground=BG_MED, activeforeground=TEXT_PRI,
                font=("Segoe UI", 9),
            ).pack(anchor="w")

        ttk.Separator(sb, orient="horizontal").pack(fill="x", pady=(6, 0))

        # ── CSV recording ──────────────────────────────────────────────────
        rec_frm = tk.Frame(sb, bg=BG_MED, padx=14, pady=10)
        rec_frm.pack(fill="x")
        tk.Label(rec_frm, text="CSV RECORDING", fg=TEXT_SEC, bg=BG_MED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, 6))

        # file path row
        path_row = tk.Frame(rec_frm, bg=BG_MED)
        path_row.pack(fill="x", pady=(0, 6))

        self._csv_path_var = tk.StringVar(value=self._default_csv_path())
        path_entry = tk.Entry(
            path_row, textvariable=self._csv_path_var,
            bg=BG_PANEL, fg=TEXT_PRI, insertbackground=TEXT_PRI,
            relief="flat", font=("Consolas", 8), bd=3,
        )
        path_entry.pack(side="left", fill="x", expand=True)

        tk.Button(
            path_row, text="…", command=self._browse_csv,
            bg=BG_PANEL, fg=TEXT_SEC, activebackground=BORDER,
            activeforeground=TEXT_PRI, font=("Segoe UI", 9),
            relief="flat", padx=4, pady=2, cursor="hand2",
        ).pack(side="left", padx=(4, 0))

        # record / stop buttons
        btn_row = tk.Frame(rec_frm, bg=BG_MED)
        btn_row.pack(fill="x")

        self._rec_btn = tk.Button(
            btn_row, text="⏺  Record", command=self._start_recording,
            bg=RECRED, fg=TEXT_PRI,
            activebackground="#b91c1c", activeforeground=TEXT_PRI,
            font=("Segoe UI", 10, "bold"), relief="flat",
            padx=10, pady=5, cursor="hand2",
        )
        self._rec_btn.pack(side="left", padx=(0, 6))

        self._stop_rec_btn = tk.Button(
            btn_row, text="⏹  Stop", command=self._stop_recording,
            bg=BG_PANEL, fg=TEXT_SEC,
            activebackground=BORDER, activeforeground=TEXT_PRI,
            font=("Segoe UI", 10), relief="flat",
            padx=10, pady=5, cursor="hand2", state="disabled",
        )
        self._stop_rec_btn.pack(side="left")

        # row count label
        self._rec_info = tk.Label(rec_frm, text="Not recording", fg=TEXT_SEC,
                                  bg=BG_MED, font=("Consolas", 8), anchor="w")
        self._rec_info.pack(fill="x", pady=(5, 0))

    # ── chart ──────────────────────────────────────────────────────────────
    def _build_chart(self, pane):
        chart_frame = tk.Frame(pane, bg=BG_DARK)
        pane.add(chart_frame, minsize=700)

        self._fig = Figure(figsize=(10, 6), facecolor=BG_DARK)
        self._ax  = self._fig.add_subplot(111, facecolor=BG_MED)
        self._style_axes()

        self._lines = []
        for i in range(NUM_CHANNELS):
            ln, = self._ax.plot([], [], color=CHANNEL_COLORS[i],
                                lw=1.3, label=f"Ch {i:02d}", alpha=0.88)
            self._lines.append(ln)

        self._ax.legend(
            loc="upper left", ncol=2, framealpha=0.18,
            facecolor=BG_PANEL, edgecolor=BORDER,
            labelcolor=TEXT_PRI, fontsize=7.5,
        )

        canvas = FigureCanvasTkAgg(self._fig, master=chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)

        tb_frame = tk.Frame(chart_frame, bg=BG_DARK)
        tb_frame.pack(fill="x")
        toolbar = NavigationToolbar2Tk(canvas, tb_frame)
        toolbar.config(bg=BG_DARK)
        toolbar.update()

        self._canvas = canvas
        self._ani = animation.FuncAnimation(
            self._fig, self._animate, interval=500,
            blit=False, cache_frame_data=False,
        )

    # ── status bar ─────────────────────────────────────────────────────────
    def _build_status_bar(self):
        bar = tk.Frame(self, bg=BG_PANEL, height=26)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self._status_dot  = tk.Label(bar, text="●", fg=RED, bg=BG_PANEL,
                                     font=("Consolas", 10))
        self._status_dot.pack(side="left", padx=(12, 4), pady=4)

        self._status_text = tk.Label(bar, text="Disconnected", fg=TEXT_SEC,
                                     bg=BG_PANEL, font=("Segoe UI", 9))
        self._status_text.pack(side="left")

        self._rec_status = tk.Label(bar, text="", fg=RECRED, bg=BG_PANEL,
                                    font=("Segoe UI", 9, "bold"))
        self._rec_status.pack(side="left", padx=20)

        self._sample_lbl = tk.Label(bar, text="Samples: 0", fg=TEXT_SEC,
                                    bg=BG_PANEL, font=("Consolas", 9))
        self._sample_lbl.pack(side="right", padx=16)

        if not NIDAQMX_AVAILABLE:
            tk.Label(bar, text="⚠  nidaqmx not found — simulation mode",
                     fg=WARN, bg=BG_PANEL,
                     font=("Segoe UI", 9)).pack(side="right", padx=12)

    # ─────────────────────────────────────────────────────────────────────────
    #  Axes style
    # ─────────────────────────────────────────────────────────────────────────
    def _style_axes(self):
        ax = self._ax
        ax.tick_params(colors=TEXT_SEC, labelsize=8)
        ax.set_xlabel("Elapsed time (s)", color=TEXT_SEC, fontsize=9)
        ax.set_ylabel("Temperature (°C)", color=TEXT_SEC, fontsize=9)
        ax.set_title("Live Thermocouple Readings — NI-9213  (K-type, 16 channels)",
                     color=TEXT_PRI, fontsize=11, pad=10)
        for spine in ax.spines.values():
            spine.set_edgecolor(BORDER)
        ax.grid(True, color=BORDER, linestyle="--", linewidth=0.5, alpha=0.6)
        self._fig.tight_layout(pad=1.5)

    # ─────────────────────────────────────────────────────────────────────────
    #  Acquisition control
    # ─────────────────────────────────────────────────────────────────────────
    def _toggle_acquisition(self):
        if self._running:
            self._stop_acquisition()
        else:
            self._start_acquisition()

    def _start_acquisition(self):
        try:
            rate = float(self._rate_var.get())
            assert 0.1 <= rate <= 100
        except Exception:
            messagebox.showerror("Invalid rate", "Sample rate must be between 0.1 and 100 Hz.")
            return

        self._start_time   = time.time()
        self._sample_total = 0
        self._running      = True

        self._worker = DAQWorker(self._device_var.get().strip(), rate, self._data_queue)
        self._worker.start()

        self._start_btn.config(text="■  Stop", bg=RED, activebackground="#b91c1c")
        self._set_status("Acquiring", GREEN)

    def _stop_acquisition(self):
        self._running = False
        if self._worker:
            self._worker.stop()
            self._worker = None
        # also stop any active recording
        if self._recorder.active:
            self._stop_recording()
        self._start_btn.config(text="▶  Start", bg=GREEN, activebackground="#2ea043")
        self._set_status("Stopped", WARN)

    def _clear_data(self):
        self._times.clear()
        for d in self._temps:
            d.clear()
        self._start_time   = time.time()
        self._sample_total = 0

    # ─────────────────────────────────────────────────────────────────────────
    #  Channel visibility
    # ─────────────────────────────────────────────────────────────────────────
    def _update_visibility(self):
        for i, ln in enumerate(self._lines):
            ln.set_visible(self._ch_visible[i].get())

    def _select_all(self):
        for v in self._ch_visible:
            v.set(True)
        self._update_visibility()

    def _deselect_all(self):
        for v in self._ch_visible:
            v.set(False)
        self._update_visibility()

    # ─────────────────────────────────────────────────────────────────────────
    #  CSV recording
    # ─────────────────────────────────────────────────────────────────────────
    def _default_csv_path(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(os.path.expanduser("~"), f"thermocouple_{ts}.csv")

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=os.path.basename(self._csv_path_var.get()),
            initialdir=os.path.dirname(self._csv_path_var.get()),
            title="Save CSV recording as…",
        )
        if path:
            self._csv_path_var.set(path)

    def _start_recording(self):
        if not self._running:
            messagebox.showwarning("Not acquiring",
                                   "Start data acquisition before recording.")
            return
        if self._recorder.active:
            return
        path = self._csv_path_var.get().strip()
        if not path:
            messagebox.showerror("No file path", "Enter or browse for a CSV file path.")
            return
        try:
            self._recorder.start(path)
        except Exception as exc:
            messagebox.showerror("Cannot open file", str(exc))
            return

        self._rec_btn.config(state="disabled")
        self._stop_rec_btn.config(state="normal")
        self._rec_info.config(text=f"Recording → {os.path.basename(path)}", fg=RECRED)
        self._rec_status.config(text="⏺  REC")

    def _stop_recording(self):
        self._recorder.stop()
        self._rec_btn.config(state="normal")
        self._stop_rec_btn.config(state="disabled")
        rows = self._recorder.row_count
        fname = os.path.basename(self._recorder.filepath)
        self._rec_info.config(
            text=f"Saved {rows:,} rows → {fname}", fg=GREEN,
        )
        self._rec_status.config(text="")
        # auto-generate a fresh default filename for next recording
        self._csv_path_var.set(self._default_csv_path())

    # ─────────────────────────────────────────────────────────────────────────
    #  Animation callback (runs every 500 ms on the main thread)
    # ─────────────────────────────────────────────────────────────────────────
    def _animate(self, _frame):
        # drain queue
        while not self._data_queue.empty():
            item = self._data_queue.get_nowait()
            if item[0] == "error":
                self._stop_acquisition()
                self._set_status(f"Error: {item[1]}", RED)
                return
            ts, temps = item
            elapsed = ts - (self._start_time or ts)
            self._times.append(elapsed)
            for i, v in enumerate(temps):
                self._temps[i].append(v)
            self._sample_total += 1

            # write to CSV if recording
            if self._recorder.active:
                self._recorder.write(ts, elapsed, temps)

        if not self._times:
            return

        times_arr = np.array(self._times)
        window    = self._window_var.get()
        t_max     = times_arr[-1]
        t_min     = max(0.0, t_max - window)
        mask      = times_arr >= t_min
        x_data    = times_arr[mask]

        all_visible = []
        for i, ln in enumerate(self._lines):
            visible = self._ch_visible[i].get()
            if visible and len(self._temps[i]) == len(times_arr):
                y = np.array(self._temps[i])[mask]
                ln.set_data(x_data, y)
                if y.size:
                    all_visible.extend(y.tolist())
                    self._temp_labels[i].config(text=f"{y[-1]:+7.2f}°C")
            else:
                ln.set_data([], [])
                if not visible:
                    self._temp_labels[i].config(text="hidden", fg=TEXT_SEC)
                else:
                    self._temp_labels[i].config(text="—", fg=CHANNEL_COLORS[i])
            # restore colour if re-enabled
            if visible:
                self._temp_labels[i].config(fg=CHANNEL_COLORS[i])

        # axis limits
        if x_data.size:
            self._ax.set_xlim(t_min, t_min + window)
        if all_visible:
            vmin, vmax = min(all_visible), max(all_visible)
            pad = max(2.0, (vmax - vmin) * 0.12)
            self._ax.set_ylim(vmin - pad, vmax + pad)
            self._stat_labels["min"].config(text=f"{vmin:.2f} °C")
            self._stat_labels["max"].config(text=f"{vmax:.2f} °C")
            self._stat_labels["avg"].config(text=f"{np.mean(all_visible):.2f} °C")
            self._stat_labels["spread"].config(text=f"{vmax - vmin:.2f} °C")

        # status bar counters
        self._sample_lbl.config(text=f"Samples: {self._sample_total:,}")
        if self._recorder.active:
            self._rec_info.config(
                text=f"Recording… {self._recorder.row_count:,} rows", fg=RECRED,
            )

        self._canvas.draw_idle()

    # ─────────────────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _set_status(self, msg: str, color: str):
        self._status_dot.config(fg=color)
        self._status_text.config(text=msg)

    def _on_close(self):
        self._stop_acquisition()
        if self._ani:
            self._ani.event_source.stop()
        self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ThermocoupleApp()
    app.mainloop()
