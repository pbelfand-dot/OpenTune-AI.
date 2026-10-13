"""Live dashboard: gauges, readouts, strip chart and simulator controls."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from opentune.core.models import CHANNELS, Sample
from opentune.core.units import UnitSystem, convert, display_unit
from opentune.gui.context import AppContext
from opentune.gui.widgets import Gauge, StripChart, ValueTile
from opentune.simulation.engine_model import FAULTS
from opentune.simulation.scenarios import SCENARIOS
from opentune.telemetry.source import TelemetrySource

TILES = (
    ("throttle", "Throttle"), ("pedal", "Pedal"), ("engine_load", "Load"), ("gear", "Gear"),
    ("coolant_temp", "Coolant"), ("oil_temp", "Oil"), ("intake_air_temp", "Intake air"),
    ("timing_advance", "Timing"), ("lambda_cmd", "Lambda (cmd)"), ("maf", "Mass air flow"),
    ("fuel_rail_pressure", "Rail pressure"), ("battery_voltage", "Voltage"), ("fuel_trim", "Fuel trim"),
    ("torque_est", "Torque (model)"),
)


class DashboardPage(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.tiles: dict[str, ValueTile] = {}
        root = QHBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(16)

        main = QVBoxLayout()
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Live Dashboard")
        title.setProperty("role", "title")
        self.subtitle = QLabel("Not connected")
        self.subtitle.setProperty("role", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        header.addWidget(QLabel("Units"))
        self.units_combo = QComboBox()
        self.units_combo.addItem("Imperial (psi, °F, mph)", UnitSystem.IMPERIAL.value)
        self.units_combo.addItem("Metric (bar, °C, km/h)", UnitSystem.METRIC.value)
        self.units_combo.currentIndexChanged.connect(self._units_changed)
        header.addWidget(self.units_combo)
        main.addLayout(header)

        gauges = QHBoxLayout()
        self.rpm_gauge = Gauge("rpm x1000", "rpm", 0, 8000, 0)
        self.boost_gauge = Gauge("Boost", "bar", -1, 2, 2)
        self.speed_gauge = Gauge("Speed", "km/h", 0, 300, 0)
        for g in (self.rpm_gauge, self.boost_gauge, self.speed_gauge):
            gauges.addWidget(g)
        main.addLayout(gauges, 3)

        grid = QGridLayout()
        grid.setSpacing(10)
        for i, (key, label) in enumerate(TILES):
            tile = ValueTile(label)
            self.tiles[key] = tile
            grid.addWidget(tile, i // 7, i % 7)
        main.addLayout(grid)

        self.chart = StripChart(30.0)
        self.chart.add_series("rpm", "RPM", 0, 8000)
        self.chart.add_series("boost", "Boost bar", -1.0, 2.0, 2)
        self.chart.add_series("throttle", "Throttle %", 0, 100)
        self.chart.add_series("timing_advance", "Timing °", -10, 50, 1)
        main.addWidget(self.chart, 2)
        root.addLayout(main, 1)

        root.addWidget(self._build_sim_panel())
        ctx.controller.sample_received.connect(self.update_sample)
        ctx.controller.connected.connect(self.on_connected)
        ctx.controller.disconnected.connect(self.on_disconnected)
        ctx.settings_changed.connect(self._apply_settings)
        self._apply_settings()

    # ------------------------------------------------------------------ simulator panel
    def _build_sim_panel(self) -> QWidget:
        self.sim_panel = QGroupBox("Simulator controls")
        self.sim_panel.setFixedWidth(280)
        lay = QVBoxLayout(self.sim_panel)
        lay.addWidget(QLabel("Driving scenario"))
        self.scenario_combo = QComboBox()
        for name, cls in SCENARIOS.items():
            self.scenario_combo.addItem(cls.description or name, name)
        self.scenario_combo.currentIndexChanged.connect(self._scenario_changed)
        lay.addWidget(self.scenario_combo)
        self.pedal_label = QLabel("Pedal: 0 %")
        lay.addWidget(self.pedal_label)
        self.pedal_slider = QSlider(Qt.Orientation.Horizontal)
        self.pedal_slider.setRange(0, 100)
        self.pedal_slider.valueChanged.connect(self._pedal_changed)
        lay.addWidget(self.pedal_slider)
        self.engine_check = QCheckBox("Engine running")
        self.engine_check.setChecked(True)
        self.engine_check.toggled.connect(self._engine_toggled)
        lay.addWidget(self.engine_check)
        lay.addSpacing(8)
        faults_label = QLabel("Inject faults")
        faults_label.setProperty("role", "section")
        lay.addWidget(faults_label)
        self.fault_checks: dict[str, QCheckBox] = {}
        for key, desc in FAULTS.items():
            box = QCheckBox(desc)
            box.toggled.connect(lambda on, k=key: self._fault_toggled(k, on))
            self.fault_checks[key] = box
            lay.addWidget(box)
        lay.addStretch(1)
        note = QLabel("Faults set realistic generic DTCs. Read them on the Diagnostics page.")
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        lay.addWidget(note)
        self.sim_panel.setVisible(False)
        return self.sim_panel

    def _vehicle(self):
        source = self.ctx.controller.source
        return source.vehicle if source is not None else None

    def _scenario_changed(self) -> None:
        vehicle = self._vehicle()
        name = self.scenario_combo.currentData()
        manual = name == "manual"
        self.pedal_slider.setEnabled(manual)
        if vehicle is None or name is None:
            return
        if manual:
            vehicle.set_manual_input(self.pedal_slider.value())
        else:
            vehicle.set_scenario(name)

    def _pedal_changed(self, value: int) -> None:
        self.pedal_label.setText(f"Pedal: {value} %")
        vehicle = self._vehicle()
        if vehicle is not None and self.scenario_combo.currentData() == "manual":
            vehicle.set_manual_input(float(value))

    def _engine_toggled(self, on: bool) -> None:
        vehicle = self._vehicle()
        if vehicle is not None:
            vehicle.set_engine_running(on)

    def _fault_toggled(self, key: str, on: bool) -> None:
        vehicle = self._vehicle()
        if vehicle is None:
            return
        if on:
            vehicle.inject_fault(key)
        else:
            vehicle.clear_fault(key)

    # ------------------------------------------------------------------ connection
    def on_connected(self, source: TelemetrySource) -> None:
        self.subtitle.setText(f"{source.profile.family_name} · {source.label}")
        self.chart.clear()
        vehicle = source.vehicle
        self.sim_panel.setVisible(vehicle is not None)
        if vehicle is not None:
            self.scenario_combo.blockSignals(True)
            index = self.scenario_combo.findData(vehicle.scenario_name)
            self.scenario_combo.setCurrentIndex(max(0, index))
            self.scenario_combo.blockSignals(False)
            self.pedal_slider.setEnabled(vehicle.scenario_name == "manual")
            self.engine_check.blockSignals(True)
            self.engine_check.setChecked(vehicle.engine_running)
            self.engine_check.blockSignals(False)
            for key, box in self.fault_checks.items():
                box.blockSignals(True)
                box.setChecked(key in vehicle.faults)
                box.blockSignals(False)
        limit = source.profile.sim.rev_limit_rpm
        self.rpm_gauge.set_range(0, 8000, warn_from=limit - 500, critical_from=limit)

    def on_disconnected(self) -> None:
        self.subtitle.setText("Not connected")
        self.sim_panel.setVisible(False)

    # ------------------------------------------------------------------ units
    @property
    def units(self) -> UnitSystem:
        return UnitSystem(self.ctx.settings.unit_system)

    def _apply_settings(self) -> None:
        index = self.units_combo.findData(self.units.value)
        self.units_combo.blockSignals(True)
        self.units_combo.setCurrentIndex(max(0, index))
        self.units_combo.blockSignals(False)
        imperial = self.units is UnitSystem.IMPERIAL
        if imperial:
            self.boost_gauge.set_range(-15, 30, "psi", 1)
            self.speed_gauge.set_range(0, 180, "mph", 0)
        else:
            self.boost_gauge.set_range(-1, 2, "bar", 2)
            self.speed_gauge.set_range(0, 300, "km/h", 0)
        for key, tile in self.tiles.items():
            unit = "%" if key == "fuel_trim" else CHANNELS[key].unit
            tile.set_unit(display_unit(unit, self.units))

    def _units_changed(self) -> None:
        self.ctx.settings.unit_system = self.units_combo.currentData()
        self.ctx.save_settings()

    # ------------------------------------------------------------------ data
    def _tile_state(self, key: str, value: float) -> str:
        t = self.ctx.profile.thresholds
        limits = {
            "coolant_temp": (t.get("coolant_warn"), t.get("coolant_critical")),
            "oil_temp": (t.get("oil_warn"), t.get("oil_critical")),
            "intake_air_temp": (t.get("iat_warn"), t.get("iat_critical")),
        }
        if key in limits:
            warn, crit = limits[key]
            if crit is not None and value >= crit:
                return "critical"
            if warn is not None and value >= warn:
                return "warn"
        if key == "battery_voltage" and value < t.get("battery_low", 12.0) and value > 5:
            return "warn"
        if key == "fuel_trim" and abs(value) >= 15:
            return "warn"
        return "normal"

    def update_sample(self, sample: Sample) -> None:
        v = sample.values
        units = self.units
        rpm = v.get("rpm")
        self.rpm_gauge.set_value(rpm)
        if "boost" in v:
            target = v.get("boost_target")
            self.boost_gauge.set_value(convert(v["boost"], "bar", units),
                                       None if target is None else convert(target, "bar", units))
        if "vehicle_speed" in v:
            self.speed_gauge.set_value(convert(v["vehicle_speed"], "km/h", units))
        values = dict(v)
        if "stft" in v and "ltft" in v:
            values["fuel_trim"] = v["stft"] + v["ltft"]
        for key, tile in self.tiles.items():
            if key not in values:
                tile.set_text("–")
                continue
            raw = values[key]
            if key == "fuel_trim":
                tile.set_text(f"{raw:+.1f}", self._tile_state(key, raw))
                continue
            ch = CHANNELS[key]
            shown = convert(raw, ch.unit, units)
            tile.set_text(f"{shown:.{ch.decimals}f}", self._tile_state(key, raw))
        self.chart.append(sample.timestamp, v)
