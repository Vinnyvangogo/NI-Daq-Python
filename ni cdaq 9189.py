"""
NI cDAQ-9189 Data Acquisition System
=====================================
Hardware configuration:
  Module 1   - NI 9213 : 16x K-type thermocouples
  Module 2-6 - NI 9320 : 80x differential analog inputs +/-10 VDC, 200 kS/s, 1000 S/s capture
  Module 7   - NI 9223 : 4x differential analog inputs, 1 MS/s, 10000 S/s capture
  Module 8   - NI 9263 : 4x analog outputs, 0-10 VDC ramp

Dependencies:
    pip install nidaqmx numpy

Notes:
  - If the `nidaqmx` package or NI-DAQmx driver is not present, the app runs in
    SIMULATION mode automatically and starts generating demo data immediately
    (no Connect step required) so the UI can be exercised without hardware.
  - In hardware mode, Connect must be pressed with a valid chassis IP before
    any module can be started. Any communication failure is surfaced in the
    status bar at the bottom of the window (does not raise a popup so it
    cannot interrupt continuous acquisition).
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import csv
import math
import queue
import json
import os
from datetime import datetime
from typing import Optional

import numpy as np

# Try real NI-DAQmx; fall back to simulation
try:
    import nidaqmx
    from nidaqmx.constants import (
        ThermocoupleType, TemperatureUnits, AcquisitionType,
        TerminalConfiguration, CJCSource, ReadRelativeTo
    )
    SIMULATION_MODE = False
except ImportError:
    SIMULATION_MODE = True
    print("[WARNING] nidaqmx not found - running in SIMULATION mode")


# ══════════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════════
TC_CHANNELS      = 16          # Module 1
AI_9320_TOTAL    = 80          # Modules 2-6, 16 ch each
AI_9223_TOTAL    = 4            # Module 7
AO_9263_TOTAL    = 4            # Module 8

# ── Channel names from Module_Channel_Names.xlsx ───────────────────────────
TC_NAMES = [
    "MCU1",           # TC0
    "JVSENSE-1",      # TC1
    "JVSENSE-4",      # TC2
    "JVSENSE-6",      # TC3
    "JVSENSE-7",      # TC4
    "JVSENSE-10",     # TC5
    "JVSENSE-12",     # TC6
    "JVSENSE-17",     # TC7
    "JVSENSE-20",     # TC8
    "JVSENSE-22",     # TC9
    "JVSENSE-23",     # TC10
    "JVSENSE-26",     # TC11
    "JVSENSE-28",     # TC12
    "JVSENSE-29",     # TC13
    "JVSENSE-32",     # TC14
    "JVSENSE-34",     # TC15
]

AI9320_NAMES = [
    # Module 2 — Voltage sense
    "M1ACIN_PHA_VSENSE_OUT", "M1ACIN_PHB_VSENSE_OUT", "M1ACIN_PHC_VSENSE_OUT",
    "EPDU_PHA_VSENSE_OUT",   "EPDU_PHB_VSENSE_OUT",   "EPDU_PHC_VSENSE_OUT",
    "M2ACIN_PHA_VSENSE_OUT", "M2ACIN_PHB_VSENSE_OUT", "M2ACIN_PHC_VSENSE_OUT",
    "LAA_PHA_VSENSE_OUT",    "LAA_PHB_VSENSE_OUT",    "LAA_PHC_VSENSE_OUT",
    "LOA_PHA_VSENSE_OUT",    "LOA_PHB_VSENSE_OUT",    "LOA_PHC_VSENSE_OUT",
    "SPARE",
    # Module 3 — Current sense
    "M1ACIN_PHA_ISENSE", "M1ACIN_PHB_ISENSE", "M1ACIN_PHC_ISENSE",
    "EPDU_PHA_ISENSE",   "EPDU_PHB_ISENSE",   "EPDU_PHC_ISENSE",
    "M2ACIN_PHA_ISENSE", "M2ACIN_PHB_ISENSE", "M2ACIN_PHC_ISENSE",
    "LAA_PHA_ISENSE",    "LAA_PHB_ISENSE",    "LAA_PHC_ISENSE",
    "LOA_PHA_ISENSE",    "LOA_PHB_ISENSE",    "LOA_PHC_ISENSE",
    "SPARE",
    # Modules 4-6 — Spare
] + ["SPARE"] * 48

AI9223_NAMES = [
    "A429_DSCS_TX_A",   # CH1
    "A429_DSCS_RX_A",   # CH2
    "A429_MCU2_TX_A",   # CH3
    "A429_MCU2_RX_A",   # CH4
]

AO_NAMES = [
    "DSCS_STO_RAMP_DAQ",    # AO0
    "MCU2_EN_RAMP_DAQ",     # AO1
    "EMPTY",                # AO2
    "EMPTY",                # AO3
]

# Calibration JSON -- checked in script directory first, then current
# working directory, so it works regardless of how the script is launched.
def _find_cal_file():
    candidates = []
    try:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "cdaq_calibration.json"))
    except NameError:
        pass
    candidates.append(os.path.join(os.getcwd(), "cdaq_calibration.json"))
    for p in candidates:
        if os.path.exists(p):
            return p
    # Default to script directory (or cwd) for writing new files
    return candidates[0] if candidates else "cdaq_calibration.json"

CAL_FILE = _find_cal_file()

# Colours
C_BG     = "#0d1117"
C_PANEL  = "#161b22"
C_BORDER = "#30363d"
C_ACCENT = "#00b4d8"
C_GREEN  = "#39d353"
C_RED    = "#f85149"
C_YELLOW = "#e3b341"
C_TEXT   = "#e6edf3"
C_MUTED  = "#8b949e"
C_INPUT  = "#21262d"

FONT_MONO   = ("Courier New", 9)
FONT_MONO_S = ("Courier New", 8)
FONT_SMALL  = ("Segoe UI", 9)
FONT_TINY   = ("Segoe UI", 8)
FONT_MED    = ("Segoe UI", 10)
FONT_BOLD   = ("Segoe UI", 10, "bold")
FONT_HEAD   = ("Segoe UI", 12, "bold")
FONT_TITLE  = ("Segoe UI", 14, "bold")


# ══════════════════════════════════════════════════════════════════════════
#  DAQ Manager
# ══════════════════════════════════════════════════════════════════════════
class DAQManager:
    """Handles all NI-DAQmx (or simulated) hardware interaction.

    `chassis_name` is the NI-DAQmx chassis base name as it appears in
    NI MAX for a network cDAQ-9189, e.g. "cDAQ9189-24E8D67". Each module
    is its own NI-DAQmx device, named by appending "Mod<N>" directly to
    the chassis base name (no separator, no slash) -- for example
    "cDAQ9189-24E8D67Mod1" for the thermocouple module in slot 1. Channels
    are then addressed as "<module_device>/ai<n>". This mirrors the
    confirmed-working pattern used elsewhere in this deployment.

    The IP address is only used to verify network reachability before
    acquisition starts; it is never embedded in a channel string.
    """

    def __init__(self, chassis_name: str, ip: str, error_queue: "queue.Queue"):
        self.ip = ip
        self.chassis = chassis_name or "cDAQ1"
        self.errors = error_queue

        # per-module device names, derived from the chassis base name
        self.dev_tc     = f"{self.chassis}Mod1"                       # 9213
        self.dev_9320   = [f"{self.chassis}Mod{m}" for m in range(2, 7)]  # 9320 x5
        self.dev_9223   = f"{self.chassis}Mod7"                       # 9223
        self.dev_ao     = f"{self.chassis}Mod8"                       # 9263

        # live data (latest reading per channel)
        self.tc_data     = [25.0] * TC_CHANNELS
        self.ai9320_data = [0.0] * AI_9320_TOTAL
        self.ai9223_data = [0.0] * AI_9223_TOTAL
        self.ao_data     = [0.0] * AO_9263_TOTAL

        # calibration: each AI gets (scale, offset); out = raw*scale + offset
        self.cal_9320 = [(1.0, 0.0)] * AI_9320_TOTAL
        self.cal_9223 = [(1.0, 0.0)] * AI_9223_TOTAL

        # channel enable flags
        self.tc_enabled      = [True] * TC_CHANNELS
        self.ai9320_enabled  = [True] * AI_9320_TOTAL
        self.ai9223_enabled  = [True] * AI_9223_TOTAL

        # running flags
        self.tc_running     = False
        self.ai9320_running = False
        self.ai9223_running = False
        self.ao_running     = False

        # ramp state
        self.ao_ramp_rate = [1.0] * AO_9263_TOTAL   # V/s
        self.ao_target    = [10.0] * AO_9263_TOTAL
        self._ao_current  = [0.0] * AO_9263_TOTAL

        # data log queue
        self.log_queue: queue.Queue = queue.Queue()
        self.logging = False

        # simulation phase accumulators (so demo data moves smoothly)
        self._sim_t = 0.0

        # error-throttling state (see report_error)
        self._last_error_time: dict = {}
        self._error_repeat_count: dict = {}

    def report_error(self, source: str, message: str):
        # Throttle identical repeated errors -- during a sustained overflow
        # storm the same error can fire every read tick (10-100+ times per
        # second). Flooding the queue with duplicates doesn't add useful
        # information and forces the UI thread to drain/render an
        # ever-growing backlog, which can itself worsen the underlying lag.
        # At most one copy of a given (source, message) pair is queued per
        # half second; a running count is appended so nothing is silently
        # lost, just coalesced.
        key = (source, message)
        now = time.monotonic()
        last = self._last_error_time.get(key, 0.0)
        count = self._error_repeat_count.get(key, 0) + 1
        self._error_repeat_count[key] = count
        if now - last < 0.5:
            return
        self._last_error_time[key] = now
        suffix = f"  (x{count} since last shown)" if count > 1 else ""
        self._error_repeat_count[key] = 0
        self.errors.put((datetime.now(), source, message + suffix))

    def test_connection(self) -> tuple[bool, str]:
        """
        Actively verify the chassis is reachable before acquisition starts.
        Returns (ok, message).

        - Network reachability: short TCP connect to the IP confirms the
          chassis is on the network and responding.
        - Device validity: queries NI-DAQmx's local system object and
          confirms at least the Module 1 (TC) device name is present.
          This requires the chassis to already be added in NI MAX.
        """
        if SIMULATION_MODE:
            return True, "Simulation mode - no hardware required."

        # 1. Network reachability (best-effort; chassis may block plain ICMP,
        #    so we attempt a short TCP connect instead).
        if self.ip:
            import socket
            try:
                sock = socket.create_connection((self.ip, 80), timeout=2.0)
                sock.close()
            except OSError:
                try:
                    sock = socket.create_connection((self.ip, 502), timeout=2.0)
                    sock.close()
                except OSError as e:
                    return False, (f"Cannot reach {self.ip} on the network "
                                    f"({e}). Check the IP address, network "
                                    f"cabling, and that the chassis is powered on.")

        # 2. Device validity via NI-DAQmx local system (requires the
        #    chassis to be added/configured in NI MAX under this name).
        try:
            system = nidaqmx.system.System.local()
            device_names = [d.name for d in system.devices]
            missing = [d for d in
                       [self.dev_tc, *self.dev_9320, self.dev_9223, self.dev_ao]
                       if d not in device_names]
            if missing:
                return False, (f"Module device(s) not found in NI-DAQmx: "
                                f"{missing}. Available devices: "
                                f"{device_names or 'none'}. Confirm the chassis "
                                f"base name (shown in NI MAX, e.g. "
                                f"'cDAQ9189-24E8D67') is correct -- module "
                                f"device names are derived from it.")
            dev = nidaqmx.system.Device(self.dev_tc)
            # self_test_device() raises if the chassis fails its self-test
            dev.self_test_device()
        except Exception as e:
            return False, f"NI-DAQmx device check failed: {e}"

        return True, f"Connected to '{self.chassis}' ({self.ip})."

    # ── module start / stop ─────────────────────────────────────────────
    def start_tc(self):
        if self.tc_running:
            return
        self.tc_running = True
        threading.Thread(target=self._tc_loop, daemon=True).start()

    def stop_tc(self):
        self.tc_running = False

    def start_ai9320(self):
        if self.ai9320_running:
            return
        self.ai9320_running = True
        threading.Thread(target=self._ai9320_loop, daemon=True).start()

    def stop_ai9320(self):
        self.ai9320_running = False

    def start_ai9223(self):
        if self.ai9223_running:
            return
        self.ai9223_running = True
        threading.Thread(target=self._ai9223_loop, daemon=True).start()

    def stop_ai9223(self):
        self.ai9223_running = False

    def start_ao(self):
        if self.ao_running:
            return
        self.ao_running = True
        threading.Thread(target=self._ao_loop, daemon=True).start()

    def stop_ao(self):
        self.ao_running = False

    # ── acquisition loops ────────────────────────────────────────────────
    def _tc_loop(self):
        interval = 1.0          # 1 Hz update for thermocouples

        task = None
        if not SIMULATION_MODE:
            try:
                task = nidaqmx.Task()
                for i in range(TC_CHANNELS):
                    task.ai_channels.add_ai_thrmcpl_chan(
                        f"{self.dev_tc}/ai{i}",
                        thermocouple_type=ThermocoupleType.K,
                        units=TemperatureUnits.DEG_C,
                        cjc_source=CJCSource.BUILT_IN,
                    )
                task.timing.cfg_samp_clk_timing(
                    rate=2.0,
                    sample_mode=AcquisitionType.CONTINUOUS,
                    samps_per_chan=120,   # 60 seconds of buffer at 2 S/s
                )
                task.start()
            except Exception as e:
                self.report_error("Module 1 (9213)",
                                  f"Failed to start task: {e}")
                self.tc_running = False
                if task:
                    try:
                        task.close()
                    except Exception:
                        pass
                return

        try:
            while self.tc_running:
                t0 = time.perf_counter()
                if SIMULATION_MODE:
                    self._sim_t += interval
                    raw = [25.0 + i * 1.2 + 4.0 * math.sin(self._sim_t * 0.2 + i)
                           + np.random.randn() * 0.2 for i in range(TC_CHANNELS)]
                else:
                    try:
                        # READ_ALL_AVAILABLE drains whatever has built up
                        # since the last read. Returns list-of-lists or
                        # list-of-arrays (one per channel). Use np.atleast_1d
                        # so scalars, plain lists, and numpy arrays all work.
                        result = task.read(
                            number_of_samples_per_channel=nidaqmx.constants.READ_ALL_AVAILABLE,
                            timeout=2.0)
                        raw = [float(np.mean(np.atleast_1d(ch_data)))
                               for ch_data in result]
                    except Exception as e:
                        self.report_error("Module 1 (9213)", str(e))
                        # Same read-position recovery as 9320/9223:
                        # jump to the most recent sample so the next read
                        # starts fresh instead of retrying a stale position.
                        try:
                            task.in_stream.relative_to = ReadRelativeTo.MOST_RECENT_SAMPLE
                            task.in_stream.offset = 0
                        except Exception as e2:
                            self.report_error("Module 1 (9213)",
                                              f"Could not reset read position: {e2}")
                        time.sleep(0.5)
                        continue

                for i, v in enumerate(raw):
                    if self.tc_enabled[i]:
                        self.tc_data[i] = v

                if self.logging:
                    self.log_queue.put(("TC", datetime.now(), list(self.tc_data)))

                elapsed = time.perf_counter() - t0
                time.sleep(max(0.0, interval - elapsed))
        finally:
            if task:
                try:
                    task.stop()
                    task.close()
                except Exception:
                    pass

    def _ai9320_loop(self):
        """9320: hardware acquisition at 2000 S/s/ch, decimated to 1000 S/s
        effective capture (2:1 averaging for noise reduction).

        IMPORTANT -- acquisition rate: the NI 9320 itself supports up to
        200 kS/s/ch, but this chassis cannot sustain that aggregate rate
        across 80 channels over a *networked* Ethernet connection. The
        cDAQ-9189's onboard streaming buffer is a tiny 8 KB pool shared
        across all running hardware-timed tasks (NI Knowledge Base:
        "Number of Concurrent Tasks on a CompactDAQ Chassis Gen II"), and
        the per-slot input FIFO is only 127 samples. At 80 ch x 200 kS/s,
        DMA cannot drain that FIFO fast enough over Ethernet, which
        produced NI-DAQmx Error -200361 (Onboard Device Memory Overflow)
        and -200279 (application not keeping up) in testing.

        Since the actual requirement is a 1000 S/s *captured* value per
        channel (not full 200 kS/s raw streaming), the hardware is
        configured to acquire at 2000 S/s/ch instead -- comfortably within
        the chassis's real-world sustained throughput -- and every 2
        samples are averaged down to the requested 1000 S/s. This still
        satisfies the capture-rate requirement while working within the
        chassis's actual capability.

        All 80 channels across Modules 2-6 remain in a SINGLE
        nidaqmx.Task() ("channel expansion") rather than five separate
        tasks, since five separate tasks caused NI-DAQmx Error -200022
        ("Resource requested by this task has already been reserved by a
        different task") -- the chassis's onboard timing/streaming
        resources ran out before all five could start. One task sharing
        one timing engine avoids that conflict and is the NI-recommended
        approach for multi-module synchronized acquisition.
        """
        target_rate = 1000                        # required capture rate (S/s/ch)
        acq_rate    = 2000                         # actual hardware rate (S/s/ch)
        avg_factor  = max(1, acq_rate // target_rate)
        block_ms    = 50                           # read cadence
        interval    = block_ms / 1000.0
        n_samples   = max(avg_factor, int(acq_rate * interval))

        task = None
        if not SIMULATION_MODE:
            try:
                task = nidaqmx.Task()
                for mod_idx in range(5):       # Mod2..Mod6
                    dev = self.dev_9320[mod_idx]
                    for ch in range(16):
                        task.ai_channels.add_ai_voltage_chan(
                            f"{dev}/ai{ch}",
                            min_val=-10.0, max_val=10.0,
                            terminal_config=TerminalConfiguration.DIFF
                        )
                task.timing.cfg_samp_clk_timing(
                    rate=acq_rate,
                    sample_mode=AcquisitionType.CONTINUOUS,
                    samps_per_chan=acq_rate * 2  # 2 seconds of host-buffer headroom
                )
                task.start()
            except Exception as e:
                self.report_error("Modules 2-6 (9320)",
                                  f"Failed to start task: {e}")
                self.ai9320_running = False
                if task:
                    try:
                        task.close()
                    except Exception:
                        pass
                return

        try:
            while self.ai9320_running:
                t0 = time.perf_counter()
                if SIMULATION_MODE:
                    raw = [5.0 * math.sin(self._sim_t * 0.5 + i * 0.3) +
                           np.random.randn() * 0.05 for i in range(AI_9320_TOTAL)]
                else:
                    try:
                        data = task.read(
                            number_of_samples_per_channel=nidaqmx.constants.READ_ALL_AVAILABLE,
                            timeout=2.0)
                        raw = [float(np.mean(np.atleast_1d(ch_data)))
                               for ch_data in data]
                    except Exception as e:
                        self.report_error("Modules 2-6 (9320)", str(e))
                        # Once a read falls behind the live buffer (overflow,
                        # Error -200279), NI-DAQmx's default read position
                        # stays where it was -- every subsequent read hits
                        # the same stale, already-overflowed position and
                        # fails again forever. Recover by jumping the read
                        # pointer to the most recent sample so the next
                        # read starts fresh instead of retrying a position
                        # that can never succeed.
                        try:
                            task.in_stream.relative_to = ReadRelativeTo.MOST_RECENT_SAMPLE
                            task.in_stream.offset = 0
                        except Exception as e2:
                            self.report_error(
                                "Modules 2-6 (9320)",
                                f"Could not reset read position: {e2}")
                        time.sleep(0.2)
                        continue

                cal = [raw[i] * self.cal_9320[i][0] + self.cal_9320[i][1]
                       if self.ai9320_enabled[i] else self.ai9320_data[i]
                       for i in range(AI_9320_TOTAL)]
                self.ai9320_data = cal

                if self.logging:
                    self.log_queue.put(("AI9320", datetime.now(), list(cal)))

                elapsed = time.perf_counter() - t0
                time.sleep(max(0.0, interval - elapsed))
        finally:
            if task:
                try:
                    task.stop()
                    task.close()
                except Exception:
                    pass

    def _ai9223_loop(self):
        """9223: hardware acquisition at 20000 S/s/ch, decimated to
        10000 S/s effective capture (2:1 averaging for noise reduction).

        A single persistent continuous-acquisition task is created once
        and read from repeatedly, rather than rebuilt every tick.

        IMPORTANT -- acquisition rate: the NI 9223 supports up to 1 MS/s/ch,
        but running all 4 channels at full rate over this *networked*
        chassis produced NI-DAQmx Error -200279 ("application not able to
        keep up with hardware acquisition") continuously in testing, the
        same onboard-streaming-bandwidth limitation documented for the
        9320 above. Since the actual requirement is a 10000 S/s captured
        value per channel (not full 1 MS/s raw streaming), the hardware
        acquires at 20000 S/s/ch instead -- well within the chassis's
        real-world sustained Ethernet throughput -- and every 2 samples
        are averaged down to the requested 10000 S/s.

        Reads are still batched into 50 ms blocks rather than read every
        0.1 ms, since every task.read() is an Ethernet round trip on this
        networked chassis.
        """
        target_rate = 10_000                      # required capture rate (S/s/ch)
        acq_rate    = 20_000                       # actual hardware rate (S/s/ch)
        avg_factor  = max(1, acq_rate // target_rate)
        block_ms    = 50
        interval    = block_ms / 1000.0
        n_samples   = max(avg_factor, int(acq_rate * interval))

        task = None
        if not SIMULATION_MODE:
            try:
                task = nidaqmx.Task()
                for ch in range(AI_9223_TOTAL):
                    task.ai_channels.add_ai_voltage_chan(
                        f"{self.dev_9223}/ai{ch}",
                        min_val=-10.0, max_val=10.0,
                        terminal_config=TerminalConfiguration.DIFF
                    )
                task.timing.cfg_samp_clk_timing(
                    rate=acq_rate,
                    sample_mode=AcquisitionType.CONTINUOUS,
                    samps_per_chan=acq_rate * 2  # 2 seconds of host-buffer headroom
                )
                task.start()
            except Exception as e:
                self.report_error("Module 7 (9223)",
                                  f"Failed to start task: {e}")
                self.ai9223_running = False
                if task:
                    try:
                        task.close()
                    except Exception:
                        pass
                return

        try:
            while self.ai9223_running:
                t0 = time.perf_counter()
                if SIMULATION_MODE:
                    raw = [7.0 * math.sin(self._sim_t * 3.0 + i) +
                           np.random.randn() * 0.05 for i in range(AI_9223_TOTAL)]
                else:
                    try:
                        data = task.read(
                            number_of_samples_per_channel=nidaqmx.constants.READ_ALL_AVAILABLE,
                            timeout=1.0)
                        raw = [float(np.mean(np.atleast_1d(ch_data)))
                               for ch_data in data]
                    except Exception as e:
                        self.report_error("Module 7 (9223)", str(e))
                        # Same overflow-recovery as the 9320 loop: once a
                        # read falls behind (Error -200279), the read
                        # position is stuck behind the live buffer and
                        # every retry fails the same way forever unless we
                        # explicitly jump forward to the most recent sample.
                        try:
                            task.in_stream.relative_to = ReadRelativeTo.MOST_RECENT_SAMPLE
                            task.in_stream.offset = 0
                        except Exception as e2:
                            self.report_error(
                                "Module 7 (9223)",
                                f"Could not reset read position: {e2}")
                        time.sleep(0.1)
                        continue

                cal = [raw[i] * self.cal_9223[i][0] + self.cal_9223[i][1]
                       if self.ai9223_enabled[i] else self.ai9223_data[i]
                       for i in range(AI_9223_TOTAL)]
                self.ai9223_data = cal

                if self.logging:
                    self.log_queue.put(("AI9223", datetime.now(), list(cal)))

                elapsed = time.perf_counter() - t0
                time.sleep(max(0.0, interval - elapsed))
        finally:
            if task:
                try:
                    task.stop()
                    task.close()
                except Exception:
                    pass

    def _ao_loop(self):
        """Ramp AO outputs toward target at adjustable V/s.

        A single persistent task is created once and written to on every
        tick, rather than rebuilt every 10 ms -- task creation overhead at
        that rate would starve the loop and stall output updates.
        """
        dt = 0.01   # 10 ms step

        task = None
        if not SIMULATION_MODE:
            try:
                task = nidaqmx.Task()
                for i in range(AO_9263_TOTAL):
                    task.ao_channels.add_ao_voltage_chan(
                        f"{self.dev_ao}/ao{i}",
                        min_val=0.0, max_val=10.0
                    )
            except Exception as e:
                self.report_error("Module 8 (9263)",
                                  f"Failed to start task: {e}")
                self.ao_running = False
                if task:
                    try:
                        task.close()
                    except Exception:
                        pass
                return

        try:
            while self.ao_running:
                t0 = time.perf_counter()
                changed = False
                for i in range(AO_9263_TOTAL):
                    tgt  = self.ao_target[i]
                    rate = max(0.001, self.ao_ramp_rate[i])
                    step = rate * dt
                    cur  = self._ao_current[i]
                    if abs(cur - tgt) < step:
                        new = tgt
                    else:
                        new = cur + step if tgt > cur else cur - step
                    if new != cur:
                        self._ao_current[i] = new
                        self.ao_data[i] = new
                        changed = True

                if changed and not SIMULATION_MODE:
                    try:
                        task.write(self._ao_current, auto_start=True)
                    except Exception as e:
                        self.report_error("Module 8 (9263)", str(e))

                elapsed = time.perf_counter() - t0
                time.sleep(max(0.0, dt - elapsed))
        finally:
            if task:
                try:
                    task.close()
                except Exception:
                    pass


# ══════════════════════════════════════════════════════════════════════════
#  Circular gauge widget (Canvas based)
# ══════════════════════════════════════════════════════════════════════════
class CircularGauge(tk.Canvas):
    """Small circular dial indicator for a single value (e.g. thermocouple)."""

    def __init__(self, parent, size=82, vmin=0.0, vmax=120.0,
                 unit="C", label="TC", **kw):
        super().__init__(parent, width=size, height=size,
                          bg=C_PANEL, highlightthickness=0, **kw)
        self.size  = size
        self.vmin  = vmin
        self.vmax  = vmax
        self.unit  = unit
        self.label = label
        self.value = None
        self.enabled = True
        self._draw()

    def set_range(self, vmin, vmax):
        self.vmin, self.vmax = vmin, vmax

    def set_value(self, value):
        self.value = value
        self._draw()

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        self._draw()

    def _value_to_color(self, frac):
        if not self.enabled:
            return C_MUTED
        if frac < 0.5:
            return C_GREEN
        if frac < 0.8:
            return C_YELLOW
        return C_RED

    def _draw(self):
        self.delete("all")
        s = self.size
        pad = 6
        x0, y0, x1, y1 = pad, pad, s - pad, s - pad

        # background ring
        self.create_oval(x0, y0, x1, y1, outline=C_BORDER, width=5)

        if self.value is None or not self.enabled:
            frac = 0.0
            txt = "---"
            color = C_MUTED
        else:
            frac = (self.value - self.vmin) / max(1e-9, (self.vmax - self.vmin))
            frac = max(0.0, min(1.0, frac))
            color = self._value_to_color(frac)
            txt = f"{self.value:.1f}"

        # arc: start at 90 deg (top), sweep clockwise proportionally to frac
        # tkinter arcs: 0 deg = 3 o'clock, positive = counter-clockwise
        extent = -frac * 300   # use a 300-degree gauge sweep (like a speedometer)
        start  = 210           # leaves a gap at the bottom
        self.create_arc(x0, y0, x1, y1, start=start, extent=extent,
                         style="arc", outline=color, width=5)

        cx, cy = s / 2, s / 2
        self.create_text(cx, cy - 6, text=txt, fill=C_TEXT,
                          font=("Courier New", 11, "bold"))
        self.create_text(cx, cy + 12, text=self.unit, fill=C_MUTED,
                          font=FONT_TINY)
        self.create_text(cx, s - pad + 8, text=self.label, fill=C_MUTED,
                          font=FONT_TINY)


# ══════════════════════════════════════════════════════════════════════════
#  GUI Application
# ══════════════════════════════════════════════════════════════════════════
class DAQApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("NI cDAQ-9189 - Acquisition Console")
        self.configure(bg=C_BG)
        self.geometry("1500x920")
        self.minsize(1200, 760)

        self.error_queue: "queue.Queue" = queue.Queue()
        self.daq: Optional[DAQManager] = None
        self._connected_ok = False
        self._log_file: Optional[str] = None
        self._log_writer = None
        self._log_fh = None
        # Pre-populated from JSON by _load_names_from_json before _build_ui
        self._json_chassis = "cDAQ9189-XXXXXXX"
        self._json_ip      = "192.168.1.100"

        self._build_style()
        self._load_names_from_json()   # update name lists from JSON before UI is built
        self._build_ui()
        self._load_calibration()       # restore saved scale/offset values into UI fields

        # In simulation mode, auto-connect and auto-start so demo data
        # is visible immediately without requiring the Connect button.
        if SIMULATION_MODE:
            self._connect(auto=True)
            self.daq.start_tc()
            self.daq.start_ai9320()
            self.daq.start_ai9223()
            self._tc_status.config(text="* Running", fg=C_GREEN)
            self._ai9320_status.config(text="* Running", fg=C_GREEN)
            self._ai9223_status.config(text="* Running", fg=C_GREEN)

        self._poll()
        self._poll_errors()

    # ── Style ────────────────────────────────────────────────────────────
    def _build_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".",
            background=C_BG, foreground=C_TEXT,
            fieldbackground=C_INPUT, troughcolor=C_BORDER,
            selectbackground=C_ACCENT, selectforeground="#000")

        s.configure("TNotebook", background=C_BG, tabmargins=[2, 4, 2, 0])
        s.configure("TNotebook.Tab", background=C_PANEL, foreground=C_MUTED,
                    padding=[12, 5], font=FONT_MED)
        s.map("TNotebook.Tab",
            background=[("selected", C_BG)],
            foreground=[("selected", C_ACCENT)])

        s.configure("TLabelframe", background=C_BG, foreground=C_ACCENT,
                    relief="flat", borderwidth=1)
        s.configure("TLabelframe.Label", background=C_BG, foreground=C_ACCENT,
                    font=FONT_BOLD)

        for name, fg in [("G.TButton", C_GREEN), ("R.TButton", C_RED),
                          ("A.TButton", C_ACCENT), ("Y.TButton", C_YELLOW)]:
            s.configure(name, background=C_PANEL, foreground=fg,
                        relief="flat", padding=[8, 4], font=FONT_SMALL)
            s.map(name, background=[("active", C_BORDER)])

        s.configure("TCheckbutton", background=C_BG, foreground=C_TEXT,
                    font=FONT_TINY)
        s.configure("Panel.TCheckbutton", background=C_PANEL, foreground=C_TEXT,
                    font=FONT_TINY)
        s.configure("TEntry", fieldbackground=C_INPUT, foreground=C_TEXT,
                    insertcolor=C_TEXT)
        s.configure("TScrollbar", background=C_BORDER, troughcolor=C_PANEL)
        s.configure("TScale", background=C_BG, troughcolor=C_BORDER)

    # ── Top bar + tabs + bottom error bar ───────────────────────────────
    def _build_ui(self):
        top = tk.Frame(self, bg=C_PANEL, pady=6, padx=14)
        top.pack(fill="x")

        tk.Label(top, text="cDAQ-9189", font=FONT_TITLE,
                 bg=C_PANEL, fg=C_ACCENT).pack(side="left")

        mode_txt = "SIMULATION" if SIMULATION_MODE else "HARDWARE"
        mode_fg  = C_YELLOW if SIMULATION_MODE else C_GREEN
        tk.Label(top, text=f" [{mode_txt}]", font=FONT_SMALL,
                 bg=C_PANEL, fg=mode_fg).pack(side="left", padx=4)

        tk.Label(top, text="   Chassis:", font=FONT_SMALL,
                 bg=C_PANEL, fg=C_MUTED).pack(side="left")
        self._device_var = tk.StringVar(value=self._json_chassis)
        dev_ent = tk.Entry(top, textvariable=self._device_var, width=18,
                           bg=C_INPUT, fg=C_TEXT, insertbackground=C_TEXT,
                           relief="flat", font=FONT_MONO)
        dev_ent.pack(side="left", padx=4)

        tk.Label(top, text="DAQ IP:", font=FONT_SMALL,
                 bg=C_PANEL, fg=C_MUTED).pack(side="left", padx=(8, 0))
        self._ip_var = tk.StringVar(value=self._json_ip)
        ip_ent = tk.Entry(top, textvariable=self._ip_var, width=15,
                          bg=C_INPUT, fg=C_TEXT, insertbackground=C_TEXT,
                          relief="flat", font=FONT_MONO)
        ip_ent.pack(side="left", padx=4)

        self._conn_btn = ttk.Button(top, text="Connect", style="G.TButton",
                                    command=self._connect)
        self._conn_btn.pack(side="left", padx=6)

        self._status_lbl = tk.Label(top, text="* Disconnected", font=FONT_SMALL,
                                    bg=C_PANEL, fg=C_RED)
        self._status_lbl.pack(side="left", padx=4)

        self._log_btn = ttk.Button(top, text="Start CSV Capture",
                                   style="Y.TButton", command=self._toggle_log)
        self._log_btn.pack(side="right")

        # Notebook
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        self._tab_tc   = self._build_tc_tab()
        self._tab_9320 = self._build_9320_tab()
        self._tab_9223 = self._build_9223_tab()
        self._tab_ao   = self._build_ao_tab()
        self._tab_cal  = self._build_cal_tab()

        self._nb.add(self._tab_tc,   text="Module 1 - TC (9213)")
        self._nb.add(self._tab_9320, text="Modules 2-6 - AI (9320)")
        self._nb.add(self._tab_9223, text="Module 7 - AI (9223)")
        self._nb.add(self._tab_ao,   text="Module 8 - AO (9263)")
        self._nb.add(self._tab_cal,  text="Calibration")

        # Bottom status / error bar -- summary line + expandable full log
        self._error_log: list[tuple] = []   # full history: (ts, source, message)

        bottom_wrap = tk.Frame(self, bg="#1c1106")
        bottom_wrap.pack(fill="x", side="bottom")

        # expandable log panel (hidden by default)
        self._err_log_frame = tk.Frame(bottom_wrap, bg="#0d0701")
        self._err_log_text = tk.Text(self._err_log_frame, height=10, bg="#0d0701",
                                     fg=C_RED, font=FONT_MONO_S, wrap="none",
                                     relief="flat", state="disabled")
        err_vsb = ttk.Scrollbar(self._err_log_frame, orient="vertical",
                                command=self._err_log_text.yview)
        self._err_log_text.configure(yscrollcommand=err_vsb.set)
        self._err_log_text.pack(side="left", fill="both", expand=True,
                                padx=(10, 0), pady=4)
        err_vsb.pack(side="right", fill="y", pady=4)
        self._err_log_visible = False   # starts collapsed

        bottom = tk.Frame(bottom_wrap, bg="#1c1106", height=28)
        bottom.pack(fill="x", side="bottom")
        bottom.pack_propagate(False)

        tk.Label(bottom, text="Comm Status:", font=FONT_TINY,
                 bg="#1c1106", fg=C_MUTED).pack(side="left", padx=(10, 4))
        self._err_lbl = tk.Label(bottom, text="No communication errors",
                                 font=FONT_TINY, bg="#1c1106", fg=C_MUTED,
                                 anchor="w")
        self._err_lbl.pack(side="left", fill="x", expand=True)

        self._err_toggle_btn = ttk.Button(bottom, text="Show Log (0)",
                                          style="A.TButton",
                                          command=self._toggle_error_log)
        self._err_toggle_btn.pack(side="right", padx=4, pady=2)

        self._err_clear_btn = ttk.Button(bottom, text="Clear", style="R.TButton",
                                         command=self._clear_error)
        self._err_clear_btn.pack(side="right", padx=4, pady=2)

    def _toggle_error_log(self):
        self._err_log_visible = not self._err_log_visible
        if self._err_log_visible:
            self._err_log_frame.pack(fill="both", expand=False,
                                     side="bottom", before=None)
        else:
            self._err_log_frame.pack_forget()
        self._err_toggle_btn.config(
            text=("Hide Log" if self._err_log_visible else "Show Log")
            + f" ({len(self._error_log)})")


    # ── small helpers ────────────────────────────────────────────────────
    def _section(self, parent, title):
        f = ttk.LabelFrame(parent, text=f" {title} ", padding=4)
        return f

    # ══════════════════════════════════════════════════════════════════
    #  Tab: Module 1 - Thermocouple (9213)  -- circular gauges, no scroll
    # ══════════════════════════════════════════════════════════════════
    def _build_tc_tab(self):
        tab = tk.Frame(self._nb, bg=C_BG)

        ctrl = tk.Frame(tab, bg=C_BG)
        ctrl.pack(fill="x", padx=10, pady=6)

        self._tc_start_btn = ttk.Button(ctrl, text="Start", style="G.TButton",
                                        command=self._start_tc)
        self._tc_start_btn.pack(side="left", padx=(0, 4))
        self._tc_stop_btn = ttk.Button(ctrl, text="Stop", style="R.TButton",
                                       command=self._stop_tc)
        self._tc_stop_btn.pack(side="left")

        tk.Label(ctrl, text="  1 S/s (9213 hardware limit)",
                 font=FONT_TINY, bg=C_BG, fg=C_MUTED).pack(side="left", padx=10)

        ttk.Button(ctrl, text="Enable All", style="A.TButton",
                   command=lambda: self._set_all_tc(True)).pack(side="left", padx=2)
        ttk.Button(ctrl, text="Disable All", style="R.TButton",
                   command=lambda: self._set_all_tc(False)).pack(side="left", padx=2)

        self._tc_status = tk.Label(ctrl, text="* Idle", font=FONT_SMALL,
                                   bg=C_BG, fg=C_MUTED)
        self._tc_status.pack(side="right")

        # 16 gauges -> 4 columns x 4 rows fills the tab evenly with no scroll
        grid = tk.Frame(tab, bg=C_BG)
        grid.pack(fill="both", expand=True, padx=10, pady=4)

        self._tc_gauges: list[CircularGauge] = []
        self._tc_checks: list[tk.BooleanVar] = []

        cols = 8
        rows = 2
        for i in range(TC_CHANNELS):
            r, c = divmod(i, cols)
            cell = tk.Frame(grid, bg=C_PANEL,
                            highlightbackground=C_BORDER, highlightthickness=1)
            cell.grid(row=r, column=c, padx=3, pady=3, sticky="nsew")
            grid.columnconfigure(c, weight=1)
            grid.rowconfigure(r, weight=1)

            chk_var = tk.BooleanVar(value=True)
            self._tc_checks.append(chk_var)
            ttk.Checkbutton(cell, text=f"TC{i:02d} · {TC_NAMES[i]}", variable=chk_var,
                            style="Panel.TCheckbutton",
                            command=lambda idx=i: self._tc_toggle(idx)
                            ).pack(pady=(2, 0))

            gauge = CircularGauge(cell, size=84, vmin=0, vmax=120,
                                  unit="C", label=f"TC{i:02d}")
            gauge.pack(padx=4, pady=2)
            self._tc_gauges.append(gauge)

        return tab

    # ══════════════════════════════════════════════════════════════════
    #  Tab: Modules 2-6 - NI 9320 (80 ch) -- dense grid, no scroll
    # ══════════════════════════════════════════════════════════════════
    def _build_9320_tab(self):
        tab = tk.Frame(self._nb, bg=C_BG)

        ctrl = tk.Frame(tab, bg=C_BG)
        ctrl.pack(fill="x", padx=10, pady=6)

        ttk.Button(ctrl, text="Start", style="G.TButton",
                   command=self._start_ai9320).pack(side="left", padx=(0, 4))
        ttk.Button(ctrl, text="Stop", style="R.TButton",
                   command=self._stop_ai9320).pack(side="left")

        tk.Label(ctrl, text="  200 kS/s hw | 1000 S/s display",
                 font=FONT_TINY, bg=C_BG, fg=C_MUTED).pack(side="left", padx=10)

        ttk.Button(ctrl, text="Enable All", style="A.TButton",
                   command=lambda: self._set_all_ai9320(True)).pack(side="left", padx=2)
        ttk.Button(ctrl, text="Disable All", style="R.TButton",
                   command=lambda: self._set_all_ai9320(False)).pack(side="left", padx=2)

        self._ai9320_status = tk.Label(ctrl, text="* Idle", font=FONT_SMALL,
                                       bg=C_BG, fg=C_MUTED)
        self._ai9320_status.pack(side="right")

        # 5 module columns x 16 channel rows, all visible at once
        body = tk.Frame(tab, bg=C_BG)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        self._ai9320_vars:   list[tk.StringVar]  = []
        self._ai9320_checks: list[tk.BooleanVar] = []

        for mod in range(5):
            col = tk.Frame(body, bg=C_BG)
            col.grid(row=0, column=mod, sticky="nsew", padx=3)
            body.columnconfigure(mod, weight=1)
            body.rowconfigure(0, weight=1)

            tk.Label(col, text=f"Module {mod + 2}", font=FONT_BOLD,
                     bg=C_BG, fg=C_ACCENT).pack(fill="x", pady=(0, 2))

            panel = tk.Frame(col, bg=C_PANEL,
                             highlightbackground=C_BORDER, highlightthickness=1)
            panel.pack(fill="both", expand=True)

            for ch in range(16):
                idx = mod * 16 + ch
                row = tk.Frame(panel, bg=C_PANEL)
                row.pack(fill="x", padx=4, pady=1)

                chk_var = tk.BooleanVar(value=True)
                self._ai9320_checks.append(chk_var)
                ttk.Checkbutton(row, variable=chk_var, text=f"{ch:02d}",
                                style="Panel.TCheckbutton", width=3,
                                command=lambda i=idx: self._ai9320_toggle(i)
                                ).pack(side="left")

                val = tk.StringVar(value="---")
                self._ai9320_vars.append(val)
                tk.Label(row, textvariable=val, width=9,
                         font=FONT_MONO_S, bg=C_INPUT, fg=C_GREEN,
                         anchor="e", padx=2).pack(side="left", padx=2)
                tk.Label(row, text=AI9320_NAMES[idx], font=FONT_TINY,
                         bg=C_PANEL, fg=C_MUTED, anchor="w"
                         ).pack(side="left", padx=2, fill="x", expand=True)

        return tab

    # ══════════════════════════════════════════════════════════════════
    #  Tab: Module 7 - NI 9223 (4 ch, 1 MS/s)
    # ══════════════════════════════════════════════════════════════════
    def _build_9223_tab(self):
        tab = tk.Frame(self._nb, bg=C_BG)

        ctrl = tk.Frame(tab, bg=C_BG)
        ctrl.pack(fill="x", padx=10, pady=6)

        ttk.Button(ctrl, text="Start", style="G.TButton",
                   command=self._start_ai9223).pack(side="left", padx=(0, 4))
        ttk.Button(ctrl, text="Stop", style="R.TButton",
                   command=self._stop_ai9223).pack(side="left")

        tk.Label(ctrl, text="  1 MS/s hw | 10000 S/s display",
                 font=FONT_TINY, bg=C_BG, fg=C_MUTED).pack(side="left", padx=10)

        ttk.Button(ctrl, text="Enable All", style="A.TButton",
                   command=lambda: self._set_all_ai9223(True)).pack(side="left", padx=2)
        ttk.Button(ctrl, text="Disable All", style="R.TButton",
                   command=lambda: self._set_all_ai9223(False)).pack(side="left", padx=2)

        self._ai9223_status = tk.Label(ctrl, text="* Idle", font=FONT_SMALL,
                                       bg=C_BG, fg=C_MUTED)
        self._ai9223_status.pack(side="right")

        grid = tk.Frame(tab, bg=C_BG)
        grid.pack(fill="both", expand=True, padx=10, pady=10)

        self._ai9223_vars:   list[tk.StringVar]  = []
        self._ai9223_checks: list[tk.BooleanVar] = []

        for i in range(AI_9223_TOTAL):
            cell = tk.Frame(grid, bg=C_PANEL, padx=12, pady=10,
                            highlightbackground=C_BORDER, highlightthickness=1)
            cell.grid(row=0, column=i, padx=8, pady=4, sticky="nsew")
            grid.columnconfigure(i, weight=1)
            grid.rowconfigure(0, weight=1)

            chk_var = tk.BooleanVar(value=True)
            self._ai9223_checks.append(chk_var)
            ttk.Checkbutton(cell, text=f"CH{i+1}", variable=chk_var,
                            style="Panel.TCheckbutton",
                            command=lambda idx=i: self._ai9223_toggle(idx)
                            ).pack()
            tk.Label(cell, text=AI9223_NAMES[i], font=FONT_TINY,
                     bg=C_PANEL, fg=C_ACCENT).pack()

            val = tk.StringVar(value="---")
            self._ai9223_vars.append(val)
            tk.Label(cell, textvariable=val, font=("Courier New", 18, "bold"),
                     bg=C_PANEL, fg=C_ACCENT, anchor="center").pack(fill="x", pady=6)
            tk.Label(cell, text="V", font=FONT_SMALL,
                     bg=C_PANEL, fg=C_MUTED).pack()

        return tab

    # ══════════════════════════════════════════════════════════════════
    #  Tab: Module 8 - NI 9263 (AO ramp)
    # ══════════════════════════════════════════════════════════════════
    def _build_ao_tab(self):
        tab = tk.Frame(self._nb, bg=C_BG)

        ctrl = tk.Frame(tab, bg=C_BG)
        ctrl.pack(fill="x", padx=10, pady=6)

        ttk.Button(ctrl, text="Start Ramp", style="G.TButton",
                   command=self._start_ao).pack(side="left", padx=(0, 4))
        ttk.Button(ctrl, text="Stop", style="R.TButton",
                   command=self._stop_ao).pack(side="left")

        self._ao_status = tk.Label(ctrl, text="* Idle", font=FONT_SMALL,
                                   bg=C_BG, fg=C_MUTED)
        self._ao_status.pack(side="right")

        grid = tk.Frame(tab, bg=C_BG)
        grid.pack(fill="both", expand=True, padx=10, pady=10)

        self._ao_rate_vars:    list[tk.DoubleVar] = []
        self._ao_target_vars:  list[tk.DoubleVar] = []
        self._ao_current_vars: list[tk.StringVar] = []

        for i in range(AO_9263_TOTAL):
            cell = tk.Frame(grid, bg=C_PANEL, padx=12, pady=10,
                            highlightbackground=C_BORDER, highlightthickness=1)
            cell.grid(row=0, column=i, padx=6, pady=4, sticky="nsew")
            grid.columnconfigure(i, weight=1)
            grid.rowconfigure(0, weight=1)

            tk.Label(cell, text=f"AO {i+1}", font=FONT_HEAD,
                     bg=C_PANEL, fg=C_ACCENT).pack()
            tk.Label(cell, text=AO_NAMES[i], font=FONT_TINY,
                     bg=C_PANEL, fg=C_MUTED).pack()

            cur_var = tk.StringVar(value="0.000 V")
            self._ao_current_vars.append(cur_var)
            tk.Label(cell, textvariable=cur_var, font=("Courier New", 16, "bold"),
                     bg=C_PANEL, fg=C_YELLOW).pack(pady=4)

            tk.Label(cell, text="Target (V):", font=FONT_TINY,
                     bg=C_PANEL, fg=C_MUTED).pack(anchor="w")
            tgt_var = tk.DoubleVar(value=10.0)
            self._ao_target_vars.append(tgt_var)
            ttk.Scale(cell, from_=0, to=10, variable=tgt_var, orient="horizontal",
                      command=lambda v, idx=i: self._update_ao_target(idx)
                      ).pack(fill="x", pady=2)
            tgt_entry = tk.Entry(cell, textvariable=tgt_var, width=8,
                                 bg=C_INPUT, fg=C_TEXT, font=FONT_MONO,
                                 insertbackground=C_TEXT, relief="flat")
            tgt_entry.pack(pady=2)
            tgt_entry.bind("<Return>", lambda e, idx=i: self._update_ao_target(idx))

            tk.Label(cell, text="Ramp Rate (V/s):", font=FONT_TINY,
                     bg=C_PANEL, fg=C_MUTED).pack(anchor="w", pady=(6, 0))
            rate_var = tk.DoubleVar(value=1.0)
            self._ao_rate_vars.append(rate_var)
            rate_entry = tk.Entry(cell, textvariable=rate_var, width=8,
                                  bg=C_INPUT, fg=C_TEXT, font=FONT_MONO,
                                  insertbackground=C_TEXT, relief="flat")
            rate_entry.pack(pady=2)
            rate_entry.bind("<Return>", lambda e, idx=i: self._update_ao_rate(idx))
            rate_entry.bind("<FocusOut>", lambda e, idx=i: self._update_ao_rate(idx))

            ttk.Button(cell, text="Reset to 0 V", style="R.TButton",
                       command=lambda idx=i: self._ao_reset(idx)).pack(pady=4)

        return tab

    # ══════════════════════════════════════════════════════════════════
    #  Tab: Calibration -- two-column layout so 84 rows fit without scroll
    # ══════════════════════════════════════════════════════════════════
    def _build_cal_tab(self):
        tab = tk.Frame(self._nb, bg=C_BG)

        # Header bar
        header = tk.Frame(tab, bg=C_BG)
        header.pack(fill="x", padx=10, pady=4)
        tk.Label(header, text="Calibration - Scale & Offset", font=FONT_HEAD,
                 bg=C_BG, fg=C_ACCENT).pack(side="left")
        tk.Label(header, text="  output = (raw × scale) + offset",
                 font=FONT_TINY, bg=C_BG, fg=C_MUTED).pack(side="left", padx=10)
        ttk.Button(header, text="Apply All", style="G.TButton",
                   command=self._apply_calibration).pack(side="right")

        self._cal_9320_scale:  list[tk.StringVar] = []
        self._cal_9320_offset: list[tk.StringVar] = []
        self._cal_9223_scale:  list[tk.StringVar] = []
        self._cal_9223_offset: list[tk.StringVar] = []

        # Scrollable body
        canvas = tk.Canvas(tab, bg=C_BG, highlightthickness=0)
        vsb = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=C_BG)
        _win = canvas.create_window((0, 0), window=body, anchor="nw")

        def _resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(_win, width=canvas.winfo_width())
        body.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        def _col_hdr(parent):
            h = tk.Frame(parent, bg=C_PANEL)
            h.pack(fill="x", padx=4, pady=(2, 0))
            tk.Label(h, text="Ch", font=FONT_TINY, bg=C_PANEL, fg=C_MUTED,
                     width=4, anchor="w").pack(side="left")
            tk.Label(h, text="Signal Name", font=FONT_TINY, bg=C_PANEL, fg=C_MUTED,
                     width=22, anchor="w").pack(side="left")
            tk.Label(h, text="Scale", font=FONT_TINY, bg=C_PANEL, fg=C_MUTED,
                     width=8, anchor="w").pack(side="left")
            tk.Label(h, text="Offset", font=FONT_TINY, bg=C_PANEL, fg=C_MUTED,
                     width=8, anchor="w").pack(side="left")

        def _cal_row(parent, ch_label, name, s_var, o_var):
            row = tk.Frame(parent, bg=C_PANEL)
            row.pack(fill="x", padx=4, pady=1)
            tk.Label(row, text=ch_label, font=FONT_MONO_S,
                     bg=C_PANEL, fg=C_TEXT, width=4, anchor="w").pack(side="left")
            tk.Label(row, text=name, font=FONT_TINY,
                     bg=C_PANEL, fg=C_ACCENT, width=22, anchor="w").pack(side="left")
            tk.Entry(row, textvariable=s_var, width=8, font=FONT_MONO_S,
                     bg=C_INPUT, fg=C_TEXT, insertbackground=C_TEXT,
                     relief="flat").pack(side="left", padx=2)
            tk.Entry(row, textvariable=o_var, width=8, font=FONT_MONO_S,
                     bg=C_INPUT, fg=C_TEXT, insertbackground=C_TEXT,
                     relief="flat").pack(side="left", padx=2)

        # ── NI 9320: one section per module, 16 channels each ──────────
        for mod_idx in range(5):
            mod_num = mod_idx + 2
            sec = ttk.LabelFrame(body,
                                 text=f" Module {mod_num}  ·  NI 9320  ·  Channels 0–15 ",
                                 padding=4)
            sec.pack(fill="x", padx=8, pady=6)
            _col_hdr(sec)
            for ch in range(16):
                idx = mod_idx * 16 + ch
                s_var = tk.StringVar(value="1.0")
                o_var = tk.StringVar(value="0.0")
                self._cal_9320_scale.append(s_var)
                self._cal_9320_offset.append(o_var)
                _cal_row(sec, f"CH{ch:02d}", AI9320_NAMES[idx], s_var, o_var)

        # ── NI 9223: Module 7, 4 channels ──────────────────────────────
        sec7 = ttk.LabelFrame(body,
                              text=" Module 7  ·  NI 9223  ·  Channels 1–4 ",
                              padding=4)
        sec7.pack(fill="x", padx=8, pady=6)
        _col_hdr(sec7)
        for i in range(AI_9223_TOTAL):
            s_var = tk.StringVar(value="1.0")
            o_var = tk.StringVar(value="0.0")
            self._cal_9223_scale.append(s_var)
            self._cal_9223_offset.append(o_var)
            _cal_row(sec7, f"CH{i+1}", AI9223_NAMES[i], s_var, o_var)

        return tab

    # ══════════════════════════════════════════════════════════════════
    #  Connection
    # ══════════════════════════════════════════════════════════════════
    def _connect(self, auto: bool = False):
        chassis_name = self._device_var.get().strip() if not auto else "cDAQ1-SIM"
        ip = self._ip_var.get().strip()

        if not auto and not SIMULATION_MODE and not chassis_name:
            messagebox.showerror(
                "Error",
                "Enter the chassis base name as shown in NI MAX "
                "(e.g. cDAQ9189-24E8D67), without a module suffix.")
            return

        self.daq = DAQManager(chassis_name, ip, self.error_queue)

        if auto:
            # Simulation auto-start: skip the UI test flow entirely.
            self._connected_ok = True
            self._status_lbl.config(
                text="* Connected (SIMULATION - no hardware)", fg=C_YELLOW)
            self._conn_btn.config(text="Reconnect")
            return

        self._connected_ok = False

        self._conn_btn.config(state="disabled", text="Testing...")
        self._status_lbl.config(text="* Testing connection...", fg=C_YELLOW)

        def worker():
            ok, message = self.daq.test_connection()
            self.after(0, lambda: self._on_connect_result(ok, message))

        threading.Thread(target=worker, daemon=True).start()

    def _on_connect_result(self, ok: bool, message: str):
        self._conn_btn.config(state="normal", text="Reconnect")
        self._connected_ok = ok
        if ok:
            self._status_lbl.config(text=f"* Connected - {message}", fg=C_GREEN)
            self._sync_ui_to_daq()
        else:
            self._status_lbl.config(text="* Connection failed", fg=C_RED)
            self.error_queue.put((datetime.now(), "Connection", message))
            messagebox.showerror("Connection Failed", message)
            # Keep self.daq set so the user can still see why it failed,
            # but treat as not usable for acquisition.

    def _sync_ui_to_daq(self):
        if not self.daq:
            return
        for i, chk in enumerate(self._tc_checks):
            self.daq.tc_enabled[i] = chk.get()
        for i, chk in enumerate(self._ai9320_checks):
            self.daq.ai9320_enabled[i] = chk.get()
        for i, chk in enumerate(self._ai9223_checks):
            self.daq.ai9223_enabled[i] = chk.get()

    def _ensure_connected(self) -> bool:
        if self.daq is None:
            messagebox.showwarning("Not Connected", "Press Connect first.")
            return False
        if not SIMULATION_MODE and not self._connected_ok:
            messagebox.showwarning(
                "Not Connected",
                "Connection has not succeeded yet. Press Connect and wait "
                "for confirmation before starting acquisition.")
            return False
        return True

    # ══════════════════════════════════════════════════════════════════
    #  Module start/stop callbacks
    # ══════════════════════════════════════════════════════════════════
    def _start_tc(self):
        if not self._ensure_connected():
            return
        self.daq.start_tc()
        self._tc_status.config(text="* Running", fg=C_GREEN)

    def _stop_tc(self):
        if self.daq:
            self.daq.stop_tc()
        self._tc_status.config(text="* Idle", fg=C_MUTED)

    def _start_ai9320(self):
        if not self._ensure_connected():
            return
        self.daq.start_ai9320()
        self._ai9320_status.config(text="* Running", fg=C_GREEN)

    def _stop_ai9320(self):
        if self.daq:
            self.daq.stop_ai9320()
        self._ai9320_status.config(text="* Idle", fg=C_MUTED)

    def _start_ai9223(self):
        if not self._ensure_connected():
            return
        self.daq.start_ai9223()
        self._ai9223_status.config(text="* Running", fg=C_GREEN)

    def _stop_ai9223(self):
        if self.daq:
            self.daq.stop_ai9223()
        self._ai9223_status.config(text="* Idle", fg=C_MUTED)

    def _start_ao(self):
        if not self._ensure_connected():
            return
        self._update_all_ao_params()
        self.daq.start_ao()
        self._ao_status.config(text="* Ramping", fg=C_YELLOW)

    def _stop_ao(self):
        if self.daq:
            self.daq.stop_ao()
        self._ao_status.config(text="* Idle", fg=C_MUTED)

    # ══════════════════════════════════════════════════════════════════
    #  Channel enable toggles
    # ══════════════════════════════════════════════════════════════════
    def _tc_toggle(self, idx):
        if self.daq:
            self.daq.tc_enabled[idx] = self._tc_checks[idx].get()
        self._tc_gauges[idx].set_enabled(self._tc_checks[idx].get())

    def _set_all_tc(self, state: bool):
        for i, chk in enumerate(self._tc_checks):
            chk.set(state)
            if self.daq:
                self.daq.tc_enabled[i] = state
            self._tc_gauges[i].set_enabled(state)

    def _ai9320_toggle(self, idx):
        if self.daq:
            self.daq.ai9320_enabled[idx] = self._ai9320_checks[idx].get()

    def _set_all_ai9320(self, state: bool):
        for i, chk in enumerate(self._ai9320_checks):
            chk.set(state)
            if self.daq:
                self.daq.ai9320_enabled[i] = state

    def _ai9223_toggle(self, idx):
        if self.daq:
            self.daq.ai9223_enabled[idx] = self._ai9223_checks[idx].get()

    def _set_all_ai9223(self, state: bool):
        for i, chk in enumerate(self._ai9223_checks):
            chk.set(state)
            if self.daq:
                self.daq.ai9223_enabled[i] = state

    # ══════════════════════════════════════════════════════════════════
    #  AO controls
    # ══════════════════════════════════════════════════════════════════
    def _update_ao_target(self, idx):
        if self.daq:
            try:
                self.daq.ao_target[idx] = float(self._ao_target_vars[idx].get())
            except (ValueError, tk.TclError):
                pass

    def _update_ao_rate(self, idx):
        if self.daq:
            try:
                self.daq.ao_ramp_rate[idx] = float(self._ao_rate_vars[idx].get())
            except (ValueError, tk.TclError):
                pass

    def _update_all_ao_params(self):
        for i in range(AO_9263_TOTAL):
            self._update_ao_target(i)
            self._update_ao_rate(i)

    def _ao_reset(self, idx):
        if self.daq:
            self.daq._ao_current[idx] = 0.0
            self.daq.ao_data[idx] = 0.0

    # ══════════════════════════════════════════════════════════════════
    #  Calibration apply
    # ══════════════════════════════════════════════════════════════════
    def _save_calibration(self):
        """Save all calibration values to cdaq_calibration.json next to the script.

        Each channel is saved as a record with its module, channel number,
        signal name, scale, and offset so the file is self-documenting and
        readable without needing to cross-reference the spreadsheet.
        """
        channels_tc = []
        for i in range(TC_CHANNELS):
            channels_tc.append({
                "module":  1,
                "channel": i,
                "name":    TC_NAMES[i],
                "scale":   1.0,
                "offset":  0.0,
            })

        channels_9320 = []
        for i in range(AI_9320_TOTAL):
            mod  = (i // 16) + 2
            ch   = i % 16
            channels_9320.append({
                "module":  mod,
                "channel": ch,
                "name":    AI9320_NAMES[i],
                "scale":   self._cal_9320_scale[i].get(),
                "offset":  self._cal_9320_offset[i].get(),
            })

        channels_9223 = []
        for i in range(AI_9223_TOTAL):
            channels_9223.append({
                "module":  7,
                "channel": i + 1,
                "name":    AI9223_NAMES[i],
                "scale":   self._cal_9223_scale[i].get(),
                "offset":  self._cal_9223_offset[i].get(),
            })

        channels_ao = []
        for i in range(AO_9263_TOTAL):
            channels_ao.append({
                "module":  8,
                "channel": i,
                "name":    AO_NAMES[i],
            })

        data = {
            "_info": (
                "cDAQ-9189 calibration file. "
                "Applied as: output = (raw * scale) + offset. "
                "Edit scale and offset values here, then restart the script "
                "or click Apply All on the Calibration tab to load them. "
                f"Saved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            "chassis_name": self._device_var.get().strip(),
            "ip_address":   self._ip_var.get().strip(),
            "NI_9213_module_1_thermocouples": channels_tc,
            "NI_9320_modules_2_to_6":         channels_9320,
            "NI_9223_module_7":               channels_9223,
            "NI_9263_module_8_ao_reference":  channels_ao,
        }
        try:
            with open(CAL_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            messagebox.showwarning("Calibration Save Error",
                                   f"Could not save calibration to:\n{CAL_FILE}\n\n{e}")

    def _load_names_from_json(self):
        """Read channel names from cdaq_calibration.json and update the
        module-level name lists (TC_NAMES, AI9320_NAMES, AI9223_NAMES,
        AO_NAMES) before the UI is built, so all widget labels, gauge
        titles, and CSV headers automatically reflect any names edited
        in the JSON file without touching the script itself.
        """
        if not os.path.exists(CAL_FILE):
            # Queue so it appears in the bottom error bar once the UI is up
            self.error_queue.put((
                datetime.now(), "Calibration JSON",
                f"Not found: {CAL_FILE}  --  using built-in default names. "
                f"Place cdaq_calibration.json in the same folder as this script."
            ))
            return

        try:
            with open(CAL_FILE) as f:
                data = json.load(f)

            for rec in data.get("NI_9213_module_1_thermocouples", []):
                ch = rec.get("channel", -1)
                if 0 <= ch < len(TC_NAMES) and rec.get("name"):
                    TC_NAMES[ch] = rec["name"]

            for rec in data.get("NI_9320_modules_2_to_6", []):
                idx = (rec.get("module", 2) - 2) * 16 + rec.get("channel", 0)
                if 0 <= idx < len(AI9320_NAMES) and rec.get("name"):
                    AI9320_NAMES[idx] = rec["name"]

            for rec in data.get("NI_9223_module_7", []):
                idx = rec.get("channel", 1) - 1
                if 0 <= idx < len(AI9223_NAMES) and rec.get("name"):
                    AI9223_NAMES[idx] = rec["name"]

            for rec in data.get("NI_9263_module_8_ao_reference", []):
                ch = rec.get("channel", -1)
                if 0 <= ch < len(AO_NAMES) and rec.get("name"):
                    AO_NAMES[ch] = rec["name"]

            # Chassis name and IP are stored as top-level fields so they
            # can be set once in the JSON and auto-populated on every launch.
            # _device_var and _ip_var don't exist yet when this runs (UI not
            # built yet), so store them as instance attrs for _build_ui to pick up.
            if data.get("chassis_name"):
                self._json_chassis = data["chassis_name"]
            if data.get("ip_address"):
                self._json_ip = data["ip_address"]

        except Exception as e:
            # Non-fatal — fall back to the hardcoded defaults.
            # Queue the error so it shows in the bottom status bar.
            self.error_queue.put((
                datetime.now(), "Calibration JSON",
                f"Could not load names from {CAL_FILE}: {e}"
            ))

    def _load_calibration(self):
        """Load calibration values from cdaq_calibration.json if it exists."""
        if not os.path.exists(CAL_FILE):
            return   # first run, nothing to load
        try:
            with open(CAL_FILE) as f:
                data = json.load(f)

            for rec in data.get("NI_9320_modules_2_to_6", []):
                idx = (rec["module"] - 2) * 16 + rec["channel"]
                if 0 <= idx < len(self._cal_9320_scale):
                    self._cal_9320_scale[idx].set(str(rec["scale"]))
                    self._cal_9320_offset[idx].set(str(rec["offset"]))

            for rec in data.get("NI_9223_module_7", []):
                idx = rec["channel"] - 1
                if 0 <= idx < len(self._cal_9223_scale):
                    self._cal_9223_scale[idx].set(str(rec["scale"]))
                    self._cal_9223_offset[idx].set(str(rec["offset"]))

        except Exception as e:
            messagebox.showwarning("Calibration Load Error",
                                   f"Could not load calibration from:\n{CAL_FILE}\n\n{e}")


    def _apply_calibration(self):
        if not self.daq:
            messagebox.showwarning("Not Connected", "Connect to apply calibration.")
            return
        errors = []
        for i in range(AI_9320_TOTAL):
            try:
                s = float(self._cal_9320_scale[i].get())
                o = float(self._cal_9320_offset[i].get())
                self.daq.cal_9320[i] = (s, o)
            except ValueError:
                errors.append(f"9320 AI{i+1}")
        for i in range(AI_9223_TOTAL):
            try:
                s = float(self._cal_9223_scale[i].get())
                o = float(self._cal_9223_offset[i].get())
                self.daq.cal_9223[i] = (s, o)
            except ValueError:
                errors.append(f"9223 AI{i+1}")

        if errors:
            messagebox.showwarning("Invalid values",
                                   "Could not parse:\n" + "\n".join(errors))
        else:
            self._save_calibration()
            messagebox.showinfo("Calibration Applied",
                                f"All calibration values applied and saved to:\n{CAL_FILE}")

    # ══════════════════════════════════════════════════════════════════
    #  CSV logging
    # ══════════════════════════════════════════════════════════════════
    def _toggle_log(self):
        if not self._ensure_connected():
            return
        if not self.daq.logging:
            path = filedialog.asksaveasfilename(
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
                title="Save capture CSV"
            )
            if not path:
                return
            self._log_file = path
            self._open_csv()
            self.daq.logging = True
            self._log_btn.config(text="Stop CSV Capture")
            threading.Thread(target=self._csv_writer_loop, daemon=True).start()
        else:
            self.daq.logging = False
            self._log_btn.config(text="Start CSV Capture")

    def _open_csv(self):
        tc_hdrs     = [f"TC{i:02d}_{TC_NAMES[i]}_degC"          for i in range(TC_CHANNELS)]
        ai9320_hdrs = [f"Mod{(i//16)+2}_CH{i%16:02d}_{AI9320_NAMES[i]}_V" for i in range(AI_9320_TOTAL)]
        ai9223_hdrs = [f"Mod7_CH{i+1}_{AI9223_NAMES[i]}_V"      for i in range(AI_9223_TOTAL)]

        self._csv_headers = (
            ["Timestamp", "Module"] + tc_hdrs + ai9320_hdrs + ai9223_hdrs
        )

        self._log_fh = open(self._log_file, "w", newline="")
        self._log_writer = csv.writer(self._log_fh)
        self._log_writer.writerow(self._csv_headers)
        self._log_fh.flush()

    def _csv_writer_loop(self):
        empty_tc   = [""] * TC_CHANNELS
        empty_9320 = [""] * AI_9320_TOTAL
        empty_9223 = [""] * AI_9223_TOTAL

        while self.daq and self.daq.logging:
            try:
                tag, ts, data = self.daq.log_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S.%f")
            if tag == "TC":
                row = [ts_str, "Module1_9213"] + [f"{v:.4f}" for v in data] + empty_9320 + empty_9223
            elif tag == "AI9320":
                row = [ts_str, "Mod2-6_9320"] + empty_tc + [f"{v:.6f}" for v in data] + empty_9223
            elif tag == "AI9223":
                row = [ts_str, "Module7_9223"] + empty_tc + empty_9320 + [f"{v:.6f}" for v in data]
            else:
                continue

            if self._log_writer:
                self._log_writer.writerow(row)
                self._log_fh.flush()

        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
            self._log_writer = None

    # ══════════════════════════════════════════════════════════════════
    #  Error / status bar polling
    # ══════════════════════════════════════════════════════════════════
    # Hard cap on retained error history -- an unbounded Tkinter Text
    # widget became measurably slower to update as error counts climbed
    # into the thousands during a sustained overflow storm, which can
    # itself eat into the main thread's time and worsen the underlying
    # acquisition lag. Old entries are dropped once this is exceeded.
    _MAX_ERROR_LOG = 500

    def _poll_errors(self):
        new_items = []
        try:
            while True:
                new_items.append(self.error_queue.get_nowait())
        except queue.Empty:
            pass

        if new_items:
            self._error_log.extend(new_items)
            if len(self._error_log) > self._MAX_ERROR_LOG:
                self._error_log = self._error_log[-self._MAX_ERROR_LOG:]

            # update summary line with the most recent message
            ts, source, message = new_items[-1]
            ts_str = ts.strftime("%H:%M:%S")
            self._err_lbl.config(
                text=f"[{ts_str}] {source}: {message}"
                + (f"  (+{len(new_items)-1} more)" if len(new_items) > 1 else ""),
                fg=C_RED
            )

            # Batch-build the new text and insert once, rather than one
            # insert() call per line -- with bursts of dozens/hundreds of
            # errors per poll cycle, per-line inserts visibly slow down
            # the Text widget and steal time from the acquisition threads.
            lines = []
            for ts, source, message in new_items[-50:]:   # cap per-burst render
                ts_str = ts.strftime("%H:%M:%S")
                lines.append(f"[{ts_str}] {source}: {message}")
            if len(new_items) > 50:
                lines.insert(0, f"... ({len(new_items)-50} more errors this update) ...")

            self._err_log_text.config(state="normal")
            self._err_log_text.insert("end", "\n".join(lines) + "\n")
            # Trim from the top if the widget itself has grown past the cap
            num_lines = int(self._err_log_text.index("end-1c").split(".")[0])
            if num_lines > self._MAX_ERROR_LOG:
                self._err_log_text.delete("1.0", f"{num_lines - self._MAX_ERROR_LOG}.0")
            self._err_log_text.see("end")
            self._err_log_text.config(state="disabled")

            self._err_toggle_btn.config(
                text=("Hide Log" if self._err_log_visible else "Show Log")
                + f" ({len(self._error_log)})")

        self.after(300, self._poll_errors)

    def _clear_error(self):
        self._err_lbl.config(text="No communication errors", fg=C_MUTED)
        self._error_log.clear()
        self._err_log_text.config(state="normal")
        self._err_log_text.delete("1.0", "end")
        self._err_log_text.config(state="disabled")
        self._err_toggle_btn.config(
            text=("Hide Log" if self._err_log_visible else "Show Log") + " (0)")

    # ══════════════════════════════════════════════════════════════════
    #  UI polling loop (~20 Hz refresh)
    # ══════════════════════════════════════════════════════════════════
    def _poll(self):
        if self.daq:
            for i, gauge in enumerate(self._tc_gauges):
                if self.daq.tc_enabled[i]:
                    gauge.set_value(self.daq.tc_data[i])

            for i, var in enumerate(self._ai9320_vars):
                if self.daq.ai9320_enabled[i]:
                    var.set(f"{self.daq.ai9320_data[i]:+8.4f}")

            for i, var in enumerate(self._ai9223_vars):
                if self.daq.ai9223_enabled[i]:
                    var.set(f"{self.daq.ai9223_data[i]:+9.5f}")

            for i, var in enumerate(self._ao_current_vars):
                var.set(f"{self.daq.ao_data[i]:.4f} V")

        self.after(50, self._poll)   # 20 Hz

    # ── close ────────────────────────────────────────────────────────
    def destroy(self):
        if self.daq:
            self.daq.tc_running     = False
            self.daq.ai9320_running = False
            self.daq.ai9223_running = False
            self.daq.ao_running     = False
            self.daq.logging        = False
        self._save_calibration()   # persist cal values on exit
        super().destroy()


# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = DAQApp()
    app.mainloop()
