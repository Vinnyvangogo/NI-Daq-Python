"""
8-Module cDAQ-9189 Monitor & Control GUI

Chassis: cDAQ-9189 (8-slot Ethernet, 7 high-performance data streams)

  Module 1 (Mod1): NI-9213, 16-ch K-type thermocouple
      75 S/s AGGREGATE across all 16 channels (hardware limit) ->
      own slow task, GUI updates every 1 s.
  Module 2 (Mod2): NI-9320, 16-ch voltage input
      CH0: Voltage divider, 199 VAC rms (phase-phase) -> ~3.5 VAC rms @ up to 2000 Hz
  Module 3 (Mod3): NI-9320, 16-ch voltage input
      CH0: Current transformer (CT), 42.86 A rms -> ~10 VAC rms @ up to 2000 Hz
      *** WARNING: 10 Vrms sine peaks at 14.14 V > the 9320's fixed
      +/-10.5 V range -> WILL CLIP. Rescale the CT burden resistor to
      <= ~6-7 Vrms. Live clipping detector included. ***
  Modules 4-6 (Mod4:6): NI-9320, 16-ch voltage input each, +/-10 VDC, RMS reported
  Module 7 (Mod7): NI-9223, 4-ch, +/-10 V, 1 MS/s/ch simultaneous voltage input
  Module 8 (Mod8): NI-9263, 4-ch, +/-10 V analog OUTPUT

TASK STRUCTURE (see explanation above the code / in chat):
  - FAST AI task: Modules 2-7 combined (5x NI-9320 + 1x NI-9223), single
    shared sample clock, fixed 100 ms RMS windows, GUI update ~100 ms.
    All fast AI modules are voltage-input types with compatible max
    rates (200 kS/s and 1 MS/s respectively), so they genuinely can
    share one synchronized task -> uses 1 of the cDAQ-9189's 7
    high-performance data streams.
  - SLOW AI task: Module 1 (thermocouples) alone, sampled well under
    its 75 S/s aggregate ceiling, GUI update every 1 s. Uses a 2nd
    data stream.
  - AO task: Module 8 alone (DAQmx requires AI and AO in separate
    tasks; there's also no readback on an output-only module). Values
    are written once at startup; edit AO_TARGET_VALUES below to
    change them. Uses a 3rd data stream.
  Total: 3 of 7 available high-performance streams used.

SIMULATION MODE:
  If no NI-DAQmx hardware is detected, or the real tasks fail to
  start, the script automatically falls back to synthetic data for
  the AI tabs (clearly flagged in the UI) and simply reports the
  commanded AO values (since AO has no readback either way).
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import numpy as np

try:
    import nidaqmx
    import nidaqmx.system
    from nidaqmx.constants import (
        AcquisitionType,
        TerminalConfiguration,
        ThermocoupleType,
        TemperatureUnits,
        CJCSource,
    )
    NIDAQMX_IMPORT_OK = True
except Exception:
    NIDAQMX_IMPORT_OK = False

# ----------------------------- CONFIG ---------------------------------
CHASSIS = "cDAQ1"                 # <-- set to the name shown in NI MAX

# --- Fast AI task (Modules 2-7) ---
FAST_SAMPLE_RATE = 100_000.0      # S/s/ch -> 50 samples/cycle @ 2000 Hz
FAST_WINDOW_SECONDS = 0.100        # 100 ms fixed RMS window / GUI update
FAST_WINDOW_SAMPLES = int(FAST_SAMPLE_RATE * FAST_WINDOW_SECONDS)

VOLT_INPUT_MIN, VOLT_INPUT_MAX = -10.5, 10.5     # NI-9320 fixed range
FAST9223_INPUT_MIN, FAST9223_INPUT_MAX = -10.0, 10.0   # NI-9223 fixed range
CLIP_WARN_THRESHOLD = 10.0

# --- Slow TC task (Module 1) ---
SLOW_SAMPLE_RATE = 4.0            # S/s/ch -> 64 S/s aggregate (< 75 S/s cap)
SLOW_WINDOW_SECONDS = 1.0          # 1 s update, per spec
SLOW_WINDOW_SAMPLES = max(1, int(SLOW_SAMPLE_RATE * SLOW_WINDOW_SECONDS))

# --- AO task (Module 8) ---
AO_TARGET_VALUES = [0.0, 0.0, 0.0, 0.0]   # volts commanded to ao0..ao3 at startup

GRID_COLS = 4


def default_channel(ch_index, unit="Vrms", sim_vrms=0.0, sim_freq=0.0):
    return {
        "scale": 1.0, "unit": unit, "label": f"CH{ch_index}",
        "sim_vrms": sim_vrms, "sim_freq": sim_freq,
    }


def build_module(slot, module_label, model, n_channels, overrides):
    channels = []
    for ch in range(n_channels):
        cfg = default_channel(ch)
        if ch in overrides:
            cfg.update(overrides[ch])
        channels.append(cfg)
    return {"slot": slot, "module_label": module_label, "model": model,
            "n_channels": n_channels, "channels": channels}


FAST_MODULES = [
    build_module(2, "Voltage (divider)", "9320", 16, overrides={
        0: {"scale": 199.0 / 3.5, "unit": "Vrms", "label": "Phase A-B",
            "sim_vrms": 3.5, "sim_freq": 1000.0},
    }),
    build_module(3, "Current (CT)", "9320", 16, overrides={
        0: {"scale": 42.86 / 10.0, "unit": "Arms", "label": "Current A",
            "sim_vrms": 10.0, "sim_freq": 1000.0},
    }),
    build_module(4, "DC Bank 1", "9320", 16, overrides={
        ch: {"sim_vrms": ((ch * 1.3) % 10) - 5} for ch in range(16)
    }),
    build_module(5, "DC Bank 2", "9320", 16, overrides={
        ch: {"sim_vrms": ((ch * 0.9) % 8) - 4} for ch in range(16)
    }),
    build_module(6, "DC Bank 3", "9320", 16, overrides={
        ch: {"sim_vrms": ((ch * 1.7) % 12) - 6} for ch in range(16)
    }),
    build_module(7, "High-Speed AI", "9223", 4, overrides={
        ch: {"sim_vrms": 1.0 + ch * 0.5, "sim_freq": 1500.0} for ch in range(4)
    }),
]

SLOW_MODULE = {
    "slot": 1, "module_label": "Thermocouples (K-type)", "model": "9213",
    "n_channels": 16,
}

AO_MODULE = {
    "slot": 8, "module_label": "Analog Output", "model": "9263",
    "n_channels": 4,
}


def build_fast_channel_metadata():
    meta = []
    for mod in FAST_MODULES:
        for ch, cfg in enumerate(mod["channels"]):
            meta.append({
                "module_slot": mod["slot"], "channel_index": ch,
                "model": mod["model"], **cfg,
            })
    return meta


def build_slow_channel_metadata():
    meta = []
    for ch in range(SLOW_MODULE["n_channels"]):
        meta.append({
            "module_slot": SLOW_MODULE["slot"], "channel_index": ch,
            "label": f"CH{ch}", "unit": "degC",
            "sim_base_c": 25.0 + ch * 1.5,
        })
    return meta


def hardware_available():
    if not NIDAQMX_IMPORT_OK:
        return False
    try:
        system = nidaqmx.system.System.local()
        return any(not dev.is_simulated for dev in system.devices)
    except Exception:
        return False


def compute_rms(data, scales):
    clipped = np.any(np.abs(data) >= CLIP_WARN_THRESHOLD, axis=1)
    rms_volts = np.sqrt(np.mean(data ** 2, axis=1))
    rms_scaled = rms_volts * scales
    return rms_volts, rms_scaled, clipped


# --------------------------- FAST AI (Modules 2-7) -----------------------

def fast_real_loop(channel_meta, scales, out_queue, stop_event):
    with nidaqmx.Task() as task:
        for mod in FAST_MODULES:
            if mod["model"] == "9320":
                vmin, vmax = VOLT_INPUT_MIN, VOLT_INPUT_MAX
            else:  # 9223
                vmin, vmax = FAST9223_INPUT_MIN, FAST9223_INPUT_MAX
            task.ai_channels.add_ai_voltage_chan(
                f"{CHASSIS}Mod{mod['slot']}/ai0:{mod['n_channels'] - 1}",
                terminal_config=TerminalConfiguration.DIFF,
                min_val=vmin, max_val=vmax,
            )
        task.timing.cfg_samp_clk_timing(
            rate=FAST_SAMPLE_RATE,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=FAST_WINDOW_SAMPLES * 4,
        )
        task.start()

        while not stop_event.is_set():
            data = np.asarray(
                task.read(number_of_samples_per_channel=FAST_WINDOW_SAMPLES)
            )
            if data.ndim == 1:
                data = data.reshape(1, -1)
            rms_volts, rms_scaled, clipped = compute_rms(data, scales)
            out_queue.put({
                "rms_volts": rms_volts, "rms_scaled": rms_scaled,
                "clipped": clipped, "sim_mode": False,
                "ts": time.strftime("%H:%M:%S"),
            })


def fast_simulation_loop(channel_meta, scales, out_queue, stop_event):
    rng = np.random.default_rng()
    t0 = 0.0
    n = len(channel_meta)
    t_axis = np.arange(FAST_WINDOW_SAMPLES) / FAST_SAMPLE_RATE

    while not stop_event.is_set():
        t = t0 + t_axis
        data = np.zeros((n, FAST_WINDOW_SAMPLES))

        for i, meta in enumerate(channel_meta):
            vrms, freq = meta["sim_vrms"], meta["sim_freq"]
            noise = rng.normal(0.0, 0.01 * max(abs(vrms), 0.05), FAST_WINDOW_SAMPLES)
            if freq > 0:
                signal = vrms * np.sqrt(2) * np.sin(2 * np.pi * freq * t) + noise
            else:
                ripple = 0.02 * max(abs(vrms), 0.1) * np.sin(2 * np.pi * 120.0 * t)
                signal = vrms + ripple + noise
            data[i, :] = np.clip(signal, VOLT_INPUT_MIN, VOLT_INPUT_MAX)

        rms_volts, rms_scaled, clipped = compute_rms(data, scales)
        out_queue.put({
            "rms_volts": rms_volts, "rms_scaled": rms_scaled,
            "clipped": clipped, "sim_mode": True,
            "ts": time.strftime("%H:%M:%S"),
        })
        t0 += FAST_WINDOW_SECONDS
        stop_event.wait(FAST_WINDOW_SECONDS)


def fast_worker(channel_meta, scales, out_queue, stop_event, use_real_hw):
    try:
        if use_real_hw:
            fast_real_loop(channel_meta, scales, out_queue, stop_event)
        else:
            fast_simulation_loop(channel_meta, scales, out_queue, stop_event)
    except Exception as exc:
        if use_real_hw and not stop_event.is_set():
            out_queue.put({"error": f"Fast AI task: {exc}"})
            fast_simulation_loop(channel_meta, scales, out_queue, stop_event)


# --------------------------- SLOW TC (Module 1) ---------------------------

def slow_real_loop(channel_meta, out_queue, stop_event):
    with nidaqmx.Task() as task:
        task.ai_channels.add_ai_thrmcpl_chan(
            f"{CHASSIS}Mod{SLOW_MODULE['slot']}/ai0:{SLOW_MODULE['n_channels'] - 1}",
            thermocouple_type=ThermocoupleType.K,
            units=TemperatureUnits.DEG_C,
            cjc_source=CJCSource.BUILT_IN,
        )
        task.timing.cfg_samp_clk_timing(
            rate=SLOW_SAMPLE_RATE,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=max(SLOW_WINDOW_SAMPLES * 4, 32),
        )
        task.start()

        while not stop_event.is_set():
            data = np.asarray(
                task.read(number_of_samples_per_channel=SLOW_WINDOW_SAMPLES)
            )
            if data.ndim == 1:
                data = data.reshape(1, -1)
            open_tc = np.any(np.isnan(data), axis=1)
            temps_c = np.nanmean(data, axis=1)
            out_queue.put({
                "temps_c": temps_c, "open_tc": open_tc, "sim_mode": False,
                "ts": time.strftime("%H:%M:%S"),
            })


def slow_simulation_loop(channel_meta, out_queue, stop_event):
    rng = np.random.default_rng()
    while not stop_event.is_set():
        temps_c = np.array([
            meta["sim_base_c"] + rng.normal(0.0, 0.2) for meta in channel_meta
        ])
        open_tc = np.zeros(len(channel_meta), dtype=bool)
        out_queue.put({
            "temps_c": temps_c, "open_tc": open_tc, "sim_mode": True,
            "ts": time.strftime("%H:%M:%S"),
        })
        stop_event.wait(SLOW_WINDOW_SECONDS)


def slow_worker(channel_meta, out_queue, stop_event, use_real_hw):
    try:
        if use_real_hw:
            slow_real_loop(channel_meta, out_queue, stop_event)
        else:
            slow_simulation_loop(channel_meta, out_queue, stop_event)
    except Exception as exc:
        if use_real_hw and not stop_event.is_set():
            out_queue.put({"error": f"Thermocouple task: {exc}"})
            slow_simulation_loop(channel_meta, out_queue, stop_event)


# ------------------------------ AO (Module 8) -----------------------------

def setup_ao(use_real_hw):
    """Write AO_TARGET_VALUES once. Returns (task_or_None, sim_mode_bool)."""
    if not use_real_hw:
        return None, True
    try:
        task = nidaqmx.Task()
        task.ao_channels.add_ao_voltage_chan(
            f"{CHASSIS}Mod{AO_MODULE['slot']}/ao0:{AO_MODULE['n_channels'] - 1}",
            min_val=-10.0, max_val=10.0,
        )
        task.write(AO_TARGET_VALUES)
        return task, False
    except Exception:
        return None, True


# --------------------------------- GUI -----------------------------------

class MonitorGUI:
    def __init__(self, root):
        self.root = root
        root.title("cDAQ-9189 (8 modules) - AC/DC/TC Monitor & AO Control")
        root.geometry("980x620")
        root.resizable(False, False)

        style = ttk.Style()
        style.configure("Cell.TLabel", font=("Consolas", 10),
                         padding=6, relief="groove", anchor="center")
        style.configure("CellName.TLabel", font=("Segoe UI", 9, "bold"),
                         anchor="center")
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))

        status_frame = ttk.Frame(root)
        status_frame.pack(fill="x", padx=10, pady=(10, 5))
        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(status_frame, textvariable=self.status_var,
                  style="Status.TLabel").pack(side="left")
        self.time_var = tk.StringVar(value="")
        ttk.Label(status_frame, textvariable=self.time_var).pack(side="right")

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)

        self.fast_cell_vars, self.fast_cell_labels = {}, {}
        self.slow_cell_vars, self.slow_cell_labels = {}, {}
        self.ao_cell_vars = {}

        # Module 1 tab (thermocouples)
        self._build_tab(
            slot=SLOW_MODULE["slot"],
            title=f"Mod{SLOW_MODULE['slot']}: {SLOW_MODULE['module_label']}",
            n_channels=SLOW_MODULE["n_channels"],
            labels=[f"CH{ch}" for ch in range(SLOW_MODULE["n_channels"])],
            var_store=self.slow_cell_vars, label_store=self.slow_cell_labels,
        )

        # Modules 2-7 tabs (fast AI)
        for mod in FAST_MODULES:
            self._build_tab(
                slot=mod["slot"],
                title=f"Mod{mod['slot']}: {mod['module_label']}",
                n_channels=mod["n_channels"],
                labels=[cfg["label"] for cfg in mod["channels"]],
                var_store=self.fast_cell_vars, label_store=self.fast_cell_labels,
            )

        # Module 8 tab (AO)
        ao_tab = ttk.Frame(self.notebook)
        self.notebook.add(ao_tab, text=f"Mod{AO_MODULE['slot']}: {AO_MODULE['module_label']}")
        ttk.Label(ao_tab, text="Commanded output (written once at startup; "
                                "edit AO_TARGET_VALUES in the script to change):",
                  style="CellName.TLabel").grid(row=0, column=0, columnspan=4,
                                                 padx=6, pady=(10, 4), sticky="w")
        for ch in range(AO_MODULE["n_channels"]):
            cell = ttk.Frame(ao_tab, padding=4)
            cell.grid(row=1, column=ch, padx=8, pady=8, sticky="nsew")
            ttk.Label(cell, text=f"AO CH{ch}", style="CellName.TLabel").pack(fill="x")
            var = tk.StringVar(value=f"{AO_TARGET_VALUES[ch]:.3f} V")
            lbl = ttk.Label(cell, textvariable=var, style="Cell.TLabel", width=14)
            lbl.pack(fill="x")
            self.ao_cell_vars[ch] = var

        # --- back-end: determine hardware, start workers ---
        use_real_hw = hardware_available()

        fast_meta = build_fast_channel_metadata()
        fast_scales = np.array([m["scale"] for m in fast_meta])
        self.fast_meta = fast_meta
        self.fast_queue = queue.Queue()
        self.stop_event = threading.Event()
        threading.Thread(
            target=fast_worker,
            args=(fast_meta, fast_scales, self.fast_queue, self.stop_event, use_real_hw),
            daemon=True,
        ).start()

        slow_meta = build_slow_channel_metadata()
        self.slow_meta = slow_meta
        self.slow_queue = queue.Queue()
        threading.Thread(
            target=slow_worker,
            args=(slow_meta, self.slow_queue, self.stop_event, use_real_hw),
            daemon=True,
        ).start()

        self.ao_task, ao_sim = setup_ao(use_real_hw)
        if ao_sim:
            for ch in range(AO_MODULE["n_channels"]):
                self.ao_cell_vars[ch].set(
                    f"{AO_TARGET_VALUES[ch]:.3f} V (SIMULATED - no HW)")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self.poll_queues)

    def _build_tab(self, slot, title, n_channels, labels, var_store, label_store):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=title)
        cols = min(GRID_COLS, n_channels)
        for ch in range(n_channels):
            row, col = divmod(ch, cols)
            cell = ttk.Frame(tab, padding=4)
            cell.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
            ttk.Label(cell, text=labels[ch], style="CellName.TLabel").pack(fill="x")
            var = tk.StringVar(value="---")
            lbl = ttk.Label(cell, textvariable=var, style="Cell.TLabel", width=18)
            lbl.pack(fill="x")
            var_store[(slot, ch)] = var
            label_store[(slot, ch)] = lbl
        for c in range(cols):
            tab.columnconfigure(c, weight=1)

    def poll_queues(self):
        # --- fast AI (Modules 2-7) ---
        try:
            while True:
                item = self.fast_queue.get_nowait()
                if "error" in item:
                    self.status_var.set(item["error"] + " -> simulation")
                    continue
                sim_mode = item["sim_mode"]
                self.status_var.set(
                    "SIMULATION MODE" if sim_mode else "LIVE - cDAQ-9189")
                self.time_var.set(f"Fast update: {item['ts']}")
                for i, meta in enumerate(self.fast_meta):
                    key = (meta["module_slot"], meta["channel_index"])
                    var, lbl = self.fast_cell_vars[key], self.fast_cell_labels[key]
                    var.set(f"{item['rms_scaled'][i]:.3f} {meta['unit']}\n"
                             f"({item['rms_volts'][i]:.3f} V)")
                    if item["clipped"][i]:
                        lbl.configure(background="#e05c5c")
                    elif sim_mode:
                        lbl.configure(background="#fff3cd")
                    else:
                        lbl.configure(background="#d4f7d4")
        except queue.Empty:
            pass

        # --- slow TC (Module 1) ---
        try:
            while True:
                item = self.slow_queue.get_nowait()
                if "error" in item:
                    self.status_var.set(item["error"] + " -> simulation")
                    continue
                sim_mode = item["sim_mode"]
                for i, meta in enumerate(self.slow_meta):
                    key = (meta["module_slot"], meta["channel_index"])
                    var, lbl = self.slow_cell_vars[key], self.slow_cell_labels[key]
                    if item["open_tc"][i]:
                        var.set("OPEN TC")
                        lbl.configure(background="#e05c5c")
                    else:
                        var.set(f"{item['temps_c'][i]:.2f} degC")
                        lbl.configure(background="#fff3cd" if sim_mode else "#d4f7d4")
        except queue.Empty:
            pass

        self.root.after(50, self.poll_queues)

    def on_close(self):
        self.stop_event.set()
        if self.ao_task is not None:
            try:
                self.ao_task.close()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    MonitorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()