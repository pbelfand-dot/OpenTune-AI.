"""Tuning engine: the simulator used as a training and test ground for calibration recommendations.

Loop: evaluate a calibration with a standard test battery (performance, efficiency, drivability and
mechanical/thermal stress) → propose a bounded change → predict its effect with a learned response
model → test it in the simulator → accept or reject → learn from the outcome (including mistakes).

Everything is simulated. Results are recommendations for further simulation and, eventually,
supervised measurement — never proof that a calibration is safe for a real vehicle.
"""
