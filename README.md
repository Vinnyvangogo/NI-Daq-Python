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

JSON file for:
    cDAQ-9189 calibration file. Applied as: output = (raw * scale) + offset.
    enabling channels with names cal info per channel
