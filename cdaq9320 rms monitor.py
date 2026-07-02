"""
AC RMS Voltage Monitor
NI cDAQ-9189 chassis, NI-9320 module in slot 2, channels ai0:2

Hardware setup assumed:
  - Chassis "cDAQ9189" (default NI-MAX name) with NI-9320 in slot/module 2
    -> device string "cDAQ9189Mod2"
  - Three channels wired to a resistive voltage divider stepping
    199 VAC rms phase-to-phase down to ~6 VAC rms at 400 Hz
  - Differential inputs, range set to +/-10 V (well inside the module's
    +/-10 V range with ~15% headroom for transients)

Method:
  - Sample continuously at 51.2 kS/s -> 128 samples per 400 Hz cycle
  - Maintain a rolling one-cycle buffer per channel
  - Recompute RMS = sqrt(mean(x^2)) every time a small slice of new
    samples arrives (sliding window), giving a high update rate while
    still averaging over a full electrical cycle (avoids ripple you'd
    get from a partial-cycle window)
  - Multiply by the voltage-divider ratio to report the actual
    phase-to-phase voltage
"""

import time
import numpy as np
import nidaqmx
from nidaqmx.constants import (
    AcquisitionType,
    TerminalConfiguration,
)

# ----------------------------- CONFIG ---------------------------------
DEVICE = "cDAQ9189-24E8D67Mod2"          # chassis "cDAQ9189", module in slot 2
CHANNELS = f"{DEVICE}/ai0:2"     # channels 0, 1, 2
NUM_CHANNELS = 3

SAMPLE_RATE = 51_200.0           # Hz -> 128 samples per 400 Hz cycle
LINE_FREQ = 400.0                # Hz, nominal AC frequency
SAMPLES_PER_CYCLE = int(round(SAMPLE_RATE / LINE_FREQ))   # 128

SLIDE_SAMPLES = 16               # new samples read/processed per update
                                  # -> RMS update rate = SAMPLE_RATE/SLIDE_SAMPLES
                                  #    = 51200/16 = 3200 Hz refresh

INPUT_RANGE = 10.0                # +/-10 V, matches NI-9320 range
DIVIDER_RATIO = 199.0 / 4.512       # scale factor back to true phase-to-phase Vrms
                                   # (adjust to your actual measured divider ratio)

TERMINAL_CONFIG = TerminalConfiguration.DIFF   # NI-9320 is differential-input

PRINT_EVERY_N_UPDATES = 200       # throttle console output (~every ~1 s at 3.2kHz)


def main():
    # Ring buffers, one row per channel, one full cycle wide
    buffers = np.zeros((NUM_CHANNELS, SAMPLES_PER_CYCLE), dtype=np.float64)
    buffer_filled = False
    update_count = 0

    with nidaqmx.Task() as task:
        task.ai_channels.add_ai_voltage_chan(
            CHANNELS,
            terminal_config=TERMINAL_CONFIG,
            min_val=-INPUT_RANGE,
            max_val=INPUT_RANGE,
        )

        # Continuous sample clock, buffer sized generously (1 second)
        task.timing.cfg_samp_clk_timing(
            rate=SAMPLE_RATE,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=int(SAMPLE_RATE),
        )

        task.start()
        print(f"Acquiring on {CHANNELS} @ {SAMPLE_RATE:.0f} S/s "
              f"({SAMPLES_PER_CYCLE} samples/cycle @ {LINE_FREQ:.0f} Hz)")
        print("Press Ctrl+C to stop.\n")

        try:
            while True:
                # data shape: [channel][sample] as list of lists
                data = task.read(number_of_samples_per_channel=SLIDE_SAMPLES)
                data = np.asarray(data)          # shape (NUM_CHANNELS, SLIDE_SAMPLES)
                if data.ndim == 1:
                    # single-channel edge case -> reshape to (1, N)
                    data = data.reshape(1, -1)

                # Slide the ring buffer: drop oldest SLIDE_SAMPLES, append newest
                buffers = np.roll(buffers, -SLIDE_SAMPLES, axis=1)
                buffers[:, -SLIDE_SAMPLES:] = data

                update_count += 1
                if not buffer_filled:
                    if update_count * SLIDE_SAMPLES < SAMPLES_PER_CYCLE:
                        continue  # wait until the buffer has a full cycle in it
                    buffer_filled = True

                # True RMS over the rolling one-cycle window
                rms_scaled = np.sqrt(np.mean(buffers ** 2, axis=1))     # at divider output
                rms_actual = rms_scaled * DIVIDER_RATIO                 # at true phase-to-phase

                if update_count % PRINT_EVERY_N_UPDATES == 0:
                    ts = time.strftime("%H:%M:%S")
                    vals = "  ".join(
                        f"CH{ch}: {rms_actual[ch]:7.2f} Vrms "
                        f"({rms_scaled[ch]:5.3f} V at divider)"
                        for ch in range(NUM_CHANNELS)
                    )
                    print(f"[{ts}] {vals}")

        except KeyboardInterrupt:
            print("\nStopped by user.")


if __name__ == "__main__":
    main()