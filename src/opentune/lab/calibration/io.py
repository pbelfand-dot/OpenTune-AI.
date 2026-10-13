"""Import/export of calibrations: JSON (complete), CSV (one table), MATLAB .mat.

The .mat export writes each table as a struct with breakpoints and data so it can
be used with MATLAB/Simulink lookup-table blocks (requires SciPy; MATLAB itself
is not needed to create the file).
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import numpy as np

from opentune.lab.calibration.tables import Calibration, CalibrationError, Table


def export_json(calibration: Calibration) -> str:
    return json.dumps(calibration.to_dict(), indent=1)


def import_json(text: str) -> Calibration:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CalibrationError(f"Not valid JSON: {exc}") from exc
    cal = Calibration.from_dict(data)
    problems = cal.validate()
    if problems:
        raise CalibrationError("Imported calibration is invalid: " + "; ".join(problems[:5]))
    return cal


def table_to_csv(table: Table) -> str:
    """1-D: two columns. 2-D: first row = column axis, first column = row axis.

    3-D tables are written as one 2-D block per breakpoint of the first axis.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# {table.id}", table.name, table.unit])
    if table.ndim == 1:
        w.writerow([f"{table.axes[0].name} ({table.axes[0].unit})", f"value ({table.unit})"])
        for x, v in zip(table.axes[0].values, table.values, strict=False):
            w.writerow([x, round(float(v), 6)])
    elif table.ndim == 2:
        w.writerow([f"{table.axes[0].name}\\{table.axes[1].name}", *table.axes[1].values])
        for x, row in zip(table.axes[0].values, table.values, strict=False):
            w.writerow([x, *[round(float(v), 6) for v in row]])
    else:
        for x, block in zip(table.axes[0].values, table.values, strict=False):
            w.writerow([f"{table.axes[0].name}={x}"])
            w.writerow([f"{table.axes[1].name}\\{table.axes[2].name}", *table.axes[2].values])
            for y, row in zip(table.axes[1].values, block, strict=False):
                w.writerow([y, *[round(float(v), 6) for v in row]])
    return buf.getvalue()


def table_from_csv(template: Table, text: str) -> np.ndarray:
    """Parse CSV written by :func:`table_to_csv` (same axes) into a value array."""
    rows = [r for r in csv.reader(io.StringIO(text)) if r and not r[0].startswith("#")]
    try:
        if template.ndim == 1:
            if [float(r[0]) for r in rows[1:]] != list(template.axes[0].values):
                raise CalibrationError("CSV axis does not match the table")
            arr = np.asarray([float(r[1]) for r in rows[1:]])
        elif template.ndim == 2:
            header = [float(v) for v in rows[0][1:]]
            if header != list(template.axes[1].values):
                raise CalibrationError("CSV column axis does not match the table")
            if [float(r[0]) for r in rows[1:]] != list(template.axes[0].values):
                raise CalibrationError("CSV row axis does not match the table")
            arr = np.asarray([[float(v) for v in r[1:]] for r in rows[1:]])
        else:
            raise CalibrationError("CSV import supports 1-D and 2-D tables")
    except (ValueError, IndexError) as exc:
        raise CalibrationError(f"Could not parse CSV: {exc}") from exc
    if arr.shape != template.shape:
        raise CalibrationError(f"CSV shape {arr.shape} does not match table shape {template.shape}")
    return arr


def export_mat(calibration: Calibration, path: str | Path) -> Path:
    from scipy.io import savemat

    payload: dict = {"engine_id": calibration.engine_id, "name": calibration.name,
                     "content_hash": calibration.content_hash()}
    for table in calibration.tables.values():
        entry = {"data": table.values, "unit": table.unit}
        for i, axis in enumerate(table.axes):
            entry[f"bp{i + 1}"] = np.asarray(axis.values)
            entry[f"bp{i + 1}_name"] = axis.name
            entry[f"bp{i + 1}_unit"] = axis.unit
        payload[f"tbl_{table.id}"] = entry
    payload["scalars"] = {s.id: s.value for s in calibration.scalars.values()}
    path = Path(path)
    savemat(path, payload, long_field_names=True)
    return path
