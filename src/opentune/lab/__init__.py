"""OpenTune Engineering Lab: virtual engine, virtual ECU and calibration suite.

Everything in this package is *simulation*. Nothing here communicates with a
physical vehicle or ECU. Key modules:

* :mod:`opentune.lab.physics` — mean-value engine plant model (air path, turbo,
  combustion torque, friction/pumping, thermal, crank dynamics) and a
  crank-angle-resolved single-zone cycle model.
* :mod:`opentune.lab.calibration` — unit-labelled multidimensional calibration
  tables with validation, editing (undo/redo), comparison and version history.
* :mod:`opentune.lab.ecu` — a virtual, torque-structured engine controller fed by
  modelled sensors, plus simulated calibration flashing.
* :mod:`opentune.lab.sim` — couples plant, sensors, ECU and load models.
* :mod:`opentune.lab.dyno` — virtual dynamometer with uncertainty estimates.
* :mod:`opentune.lab.control`, :mod:`opentune.lab.analysis` — control-system
  experiments, sensitivity analysis, optimisation and the AI analyst.
* :mod:`opentune.lab.api` — FastAPI backend serving the browser UI.

Every number carries a :class:`~opentune.lab.provenance.Provenance` so that
documented specifications are never confused with modelling assumptions.
"""

LAB_VERSION = "0.2.0"
