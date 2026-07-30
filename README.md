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
  - Added functionalility in JSON file and GUI to turn channels on module 2-6 
    to RMS or RAW calibrated data to the display and record to CSV file. 

JSON file for:
    cDAQ-9189 calibration file. Applied as: output = (raw * scale) + offset.
    enabling channels with names cal info per channel

What changed in _ai9320_loop:

	•	n_samples is now avg_factor (raw samples per channel to average), computed straight from capture_rate_hz vs hw_sample_rate_hz — no longer a dead value.
	•	The read call requests exactly n_samples per channel instead of READ_ALL_AVAILABLE. task.read() now blocks until that many raw samples have actually arrived, so every reading really is an avg_factor-sample average, and the loop naturally ticks at capture_rate_hz.
	•	Removed the old coupling to csv_capture_rate_hz for pacing this loop. It was never the right dependency — _csv_writer_loop and _poll_ai9320 already read self.ai9320_data independently on their own schedules, so this acquisition loop is now free to tick purely at the rate capture_rate_hz specifies.

Net effect: capture_rate_hz now does what its name and the docstring always claimed — it sets the real averaging window on the raw hardware samples, deterministically, rather than an averaging window that quietly depended on how fast the CSV/GUI happened to be polling