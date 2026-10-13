"""Headless GUI tests (Qt offscreen platform). Skipped if PySide6 is not installed."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from opentune.config import AppPaths, Settings  # noqa: E402
from opentune.core.models import Sample  # noqa: E402
from opentune.gui.context import AppContext  # noqa: E402
from opentune.gui.main_window import MainWindow  # noqa: E402
from opentune.gui.theme import apply_theme  # noqa: E402
from opentune.gui.widgets import Gauge, StripChart, ValueTile  # noqa: E402


@pytest.fixture
def ctx(qapp, tmp_path):
    apply_theme(qapp)
    context = AppContext(AppPaths(tmp_path / "gui-data"), Settings(sample_rate_hz=50, unit_system="metric"))
    yield context
    context.shutdown()


@pytest.fixture
def window(qtbot, ctx):
    win = MainWindow(ctx, auto_connect=False)
    qtbot.addWidget(win)
    win.show()
    return win


def connect_and_wait(qtbot, ctx, scenario="wot_pull"):
    with qtbot.waitSignal(ctx.controller.connected, timeout=5000):
        ctx.connect(scenario)
    with qtbot.waitSignal(ctx.controller.sample_received, timeout=5000):
        pass


# ---------------------------------------------------------------------------- widgets
def test_gauge_and_tile_render(qtbot):
    gauge = Gauge("Boost", "bar", -1, 2, 2, warn_from=1.5, critical_from=1.8)
    qtbot.addWidget(gauge)
    gauge.resize(240, 240)
    gauge.set_value(1.9, marker=1.2)
    assert gauge.value == 1.9
    assert not gauge.grab().isNull()
    gauge.set_value(None)
    assert not gauge.grab().isNull()

    tile = ValueTile("Coolant", "°C")
    qtbot.addWidget(tile)
    tile.set_text("121", "critical")
    assert tile.text == "121" and tile.state == "critical"


def test_strip_chart_trims_window(qtbot):
    chart = StripChart(window_s=5)
    qtbot.addWidget(chart)
    chart.add_series("rpm", "RPM", 0, 8000)
    for i in range(200):
        chart.append(i * 0.1, {"rpm": 1000 + i})
    times = [t for t, _ in chart.series["rpm"].points]
    assert times[-1] - times[0] <= 5.5 + 1e-9
    chart.resize(600, 200)
    assert not chart.grab().isNull()


# ---------------------------------------------------------------------------- app flows
def test_main_window_pages(window):
    assert set(window.pages) == {"dashboard", "diagnostics", "logs", "calibrations", "analysis",
                                 "programming", "connection"}
    window.show_page("calibrations")
    assert window.stack.currentWidget() is window.pages["calibrations"]


def test_live_dashboard_updates(qtbot, window, ctx):
    connect_and_wait(qtbot, ctx)
    dash = window.pages["dashboard"]
    qtbot.waitUntil(lambda: dash.rpm_gauge.value is not None and dash.rpm_gauge.value > 0, timeout=5000)
    assert dash.sim_panel.isVisible()
    assert dash.tiles["coolant_temp"].text != "–"
    # Switch to manual driving and floor it.
    dash.scenario_combo.setCurrentIndex(dash.scenario_combo.findData("manual"))
    dash.pedal_slider.setValue(100)
    qtbot.waitUntil(lambda: (ctx.controller.latest.values.get("vehicle_speed", 0)) > 20, timeout=10000)
    # Units toggle converts the boost gauge range.
    dash.units_combo.setCurrentIndex(dash.units_combo.findData("imperial"))
    assert dash.boost_gauge.unit == "psi"
    assert ctx.settings.unit_system == "imperial"


def test_fault_to_dtc_flow(qtbot, window, ctx):
    connect_and_wait(qtbot, ctx, "cruise")
    dash = window.pages["dashboard"]
    dash.fault_checks["lean"].setChecked(True)
    assert "lean" in ctx.controller.source.vehicle.faults
    ctx.controller.source.vehicle.advance(40)  # fast-forward the simulated car
    diag = window.pages["diagnostics"]
    diag.read_dtcs()
    qtbot.waitUntil(lambda: diag.last_report is not None, timeout=5000)
    assert "P0171" in {d.code for d in diag.last_report.stored}
    assert diag.mil_label.text() == "MIL: ON"
    diag.last_report = None
    diag.perform_clear()
    qtbot.waitUntil(lambda: diag.last_report is not None, timeout=5000)
    assert diag.last_report.stored == []


def test_logging_then_analysis(qtbot, window, ctx):
    connect_and_wait(qtbot, ctx)
    logs = window.pages["logs"]
    logs.record_btn.setChecked(True)
    assert ctx.controller.is_logging
    qtbot.waitUntil(lambda: ctx.controller.log_rows >= 20, timeout=10000)
    logs.record_btn.setChecked(False)
    assert not ctx.controller.is_logging
    assert logs.table.rowCount() == 1
    logs.table.selectRow(0)
    analysis = window.pages["analysis"]
    logs.analyze_selected()
    assert window.stack.currentWidget() is analysis
    qtbot.waitUntil(lambda: analysis.report is not None, timeout=10000)
    assert analysis.report.sample_count >= 20
    assert "Health score" in analysis.score.text()


def test_calibration_edit_and_simulated_flash(qtbot, window, ctx):
    connect_and_wait(qtbot, ctx, "idle")
    cals = window.pages["calibrations"]
    cals.create_stock()
    assert cals.table.rowCount() == 1
    stock = ctx.library.list()[0]

    # Make a modified version the way the map editor does.
    from opentune.gui.pages.calibrations import MapEditorDialog

    dialog = MapEditorDialog(ctx.library.load_simcal(stock.id), stock.name)
    qtbot.addWidget(dialog)
    editor = dialog.editors["boost_target"]
    editor.selectAll()
    editor.scale_selection(1.0, 0.1)
    assert dialog.calibration.validate() == []
    dialog.calibration.version = "SIM-B58-MOD001"
    tuned = ctx.library.add_simcal(dialog.calibration, "tuned", origin="simulated-modified", parent_id=stock.id)
    ctx.library_changed.emit()

    prog = window.pages["programming"]
    prog.image_combo.setCurrentIndex(prog.image_combo.findData(tuned.id))
    assert prog.validate() == ["Engine is running — switch the engine off (ignition on) first"]
    prog._toggle_engine()
    assert prog.validate() == []
    prog.flash(confirm=False)
    qtbot.waitUntil(lambda: prog.last_result is not None, timeout=10000)
    assert prog.last_result.success, prog.last_result.steps
    assert ctx.controller.source.vehicle.calibration.version == "SIM-B58-MOD001"
    assert prog.software_label.text() == "SIM-B58-MOD001"
    assert any(r.origin == "ecu-backup" for r in ctx.library.list())


def test_programming_power_loss_shows_recovery(qtbot, window, ctx):
    cals = window.pages["calibrations"]
    cals.create_stock()
    prog = window.pages["programming"]
    prog.refresh_images()
    prog.vehicle().set_engine_running(False)
    prog.power_loss_check.setChecked(True)
    prog.power_loss_spin.setValue(40)
    prog.flash(confirm=False)
    qtbot.waitUntil(lambda: prog.last_result is not None, timeout=10000)
    assert not prog.last_result.success
    assert prog.state_label.text() == "RECOVERY"


def test_connection_page_switches_to_obd_emulation(qtbot, window, ctx):
    conn = window.pages["connection"]
    conn.kind_buttons["simulator-obd"].setChecked(True)
    conn.engine_combo.setCurrentIndex(conn.engine_combo.findData("B48"))
    with qtbot.waitSignal(ctx.controller.connected, timeout=10000):
        conn.connect_clicked()
    assert ctx.settings.engine == "B48" and ctx.settings.connection == "simulator-obd"
    with qtbot.waitSignal(ctx.controller.sample_received, timeout=5000) as blocker:
        pass
    sample: Sample = blocker.args[0]
    assert "rpm" in sample.values and "boost_target" not in sample.values
    assert "B48" in conn.status.text()
    ctx.controller.disconnect_source()
    assert not ctx.controller.is_connected


def test_real_adapter_without_port_is_refused(qtbot, window, ctx):
    conn = window.pages["connection"]
    conn.kind_buttons["elm327"].setChecked(True)
    conn.port_combo.setCurrentText("")
    conn.connect_clicked()
    assert "serial port" in conn.status.text()
    assert not ctx.controller.is_connected


def test_bad_serial_port_reports_error(qtbot, window, ctx):
    conn = window.pages["connection"]
    conn.kind_buttons["elm327"].setChecked(True)
    conn.port_combo.setCurrentText("/dev/does-not-exist")
    with qtbot.waitSignal(ctx.controller.error, timeout=10000) as blocker:
        conn.connect_clicked()
    assert "failed" in blocker.args[0].lower()
