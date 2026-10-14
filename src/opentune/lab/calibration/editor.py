"""Calibration editing with validation and undo/redo.

Every edit is a reversible command. Values are validated against the table's
allowed range before anything changes, so an invalid edit never leaves a
calibration half-modified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from opentune.lab.calibration.tables import Calibration, CalibrationError

Region = tuple[tuple[int, int], ...]  # per axis: (start, stop) inclusive-exclusive


@dataclass
class EditCommand:
    description: str
    kind: str  # "cells" | "axis" | "scalar"
    target: str
    before: Any
    after: Any
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"description": self.description, "kind": self.kind, "target": self.target}


def _region_slices(table, region: Region | None) -> tuple[slice, ...]:
    if region is None:
        return tuple(slice(None) for _ in table.axes)
    if len(region) != table.ndim:
        raise CalibrationError(f"Region must have {table.ndim} ranges")
    out = []
    for (start, stop), n in zip(region, table.shape, strict=False):
        if not (0 <= start < stop <= n):
            raise CalibrationError(f"Region {start}:{stop} outside axis of length {n}")
        out.append(slice(start, stop))
    return tuple(out)


class CalibrationEditor:
    """Edits one calibration in place with an undo/redo history."""

    def __init__(self, calibration: Calibration, max_history: int = 500) -> None:
        self.calibration = calibration
        self.undo_stack: list[EditCommand] = []
        self.redo_stack: list[EditCommand] = []
        self.max_history = max_history

    # ------------------------------------------------------------------ internals
    def _push(self, cmd: EditCommand) -> EditCommand:
        self.undo_stack.append(cmd)
        if len(self.undo_stack) > self.max_history:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        return cmd

    def _apply_values(self, table_id: str, new_values: np.ndarray, description: str) -> EditCommand:
        table = self.calibration.table(table_id)
        new_values = np.round(np.asarray(new_values, dtype=float), 6)
        if new_values.shape != table.values.shape:
            raise CalibrationError("Edited values have the wrong shape")
        if not np.all(np.isfinite(new_values)):
            raise CalibrationError("Values must be finite numbers")
        lo, hi = float(new_values.min()), float(new_values.max())
        if lo < table.minimum or hi > table.maximum:
            raise CalibrationError(
                f"{table.name}: result {lo:g}…{hi:g} {table.unit} exceeds the allowed range "
                f"{table.minimum:g}…{table.maximum:g} {table.unit}")
        before = table.values.copy()
        table.values = new_values
        table.invalidate()
        return self._push(EditCommand(description, "cells", table_id, before, new_values.copy()))

    # ------------------------------------------------------------------ cell edits
    def set_cell(self, table_id: str, index: tuple[int, ...], value: float) -> EditCommand:
        table = self.calibration.table(table_id)
        values = table.values.copy()
        try:
            values[tuple(index)] = float(value)
        except (IndexError, ValueError, TypeError) as exc:
            raise CalibrationError(f"Invalid cell {index}: {exc}") from exc
        return self._apply_values(table_id, values, f"{table.name}: set cell {tuple(index)} = {value:g}")

    def set_cells(self, table_id: str, cells: list[tuple[tuple[int, ...], float]]) -> EditCommand:
        table = self.calibration.table(table_id)
        values = table.values.copy()
        for index, value in cells:
            values[tuple(index)] = float(value)
        return self._apply_values(table_id, values, f"{table.name}: set {len(cells)} cells")

    def apply_region(self, table_id: str, operation: str, amount: float = 0.0,
                     region: Region | None = None) -> EditCommand:
        """Apply ``set`` / ``add`` / ``multiply`` / ``percent`` / ``smooth`` / ``interpolate`` to a region."""
        table = self.calibration.table(table_id)
        values = table.values.copy()
        sl = _region_slices(table, region)
        block = values[sl]
        if operation == "set":
            block[...] = amount
        elif operation == "add":
            block += amount
        elif operation == "multiply":
            block *= amount
        elif operation == "percent":
            block *= 1.0 + amount / 100.0
        elif operation == "smooth":
            block[...] = _smooth(block)
        elif operation == "interpolate":
            block[...] = _interpolate_corners(block)
        else:
            raise CalibrationError(f"Unknown operation {operation!r}")
        values[sl] = block
        return self._apply_values(table_id, values, f"{table.name}: {operation} {amount:g} on region {region}")

    def replace_values(self, table_id: str, values: list | np.ndarray, description: str = "") -> EditCommand:
        table = self.calibration.table(table_id)
        return self._apply_values(table_id, np.asarray(values, dtype=float), description or f"{table.name}: replace")

    # ------------------------------------------------------------------ axes and scalars
    def set_axis_value(self, table_id: str, axis_index: int, point: int, value: float,
                       resample: bool = True) -> EditCommand:
        """Change one axis breakpoint; by default re-interpolate the data onto the new axis."""
        table = self.calibration.table(table_id)
        if not 0 <= axis_index < table.ndim:
            raise CalibrationError("Axis index out of range")
        axis = table.axes[axis_index]
        new_axis = list(axis.values)
        if not 0 <= point < len(new_axis):
            raise CalibrationError("Breakpoint index out of range")
        new_axis[point] = float(value)
        if any(b <= a for a, b in zip(new_axis, new_axis[1:], strict=False)):
            raise CalibrationError(f"Axis '{axis.name}' must stay strictly increasing")
        if not axis.minimum <= value <= axis.maximum:
            raise CalibrationError(f"Axis value {value:g} outside {axis.minimum:g}…{axis.maximum:g} {axis.unit}")
        before = (list(axis.values), table.values.copy())
        new_values = table.resample([new_axis if i == axis_index else list(a.values)
                                     for i, a in enumerate(table.axes)]) if resample else table.values.copy()
        axis.values = new_axis
        table.values = np.round(new_values, 6)
        table.invalidate()
        return self._push(EditCommand(f"{table.name}: axis {axis.name}[{point}] = {value:g}", "axis", table_id,
                                      before, (list(new_axis), table.values.copy()), {"axis_index": axis_index}))

    def set_scalar(self, scalar_id: str, value: float) -> EditCommand:
        scalar = self.calibration.scalars.get(scalar_id)
        if scalar is None:
            raise CalibrationError(f"Unknown scalar {scalar_id!r}")
        value = float(value)
        if not scalar.minimum <= value <= scalar.maximum:
            raise CalibrationError(f"{scalar.name}: {value:g} outside {scalar.minimum:g}…{scalar.maximum:g} "
                                   f"{scalar.unit}")
        before = scalar.value
        scalar.value = value
        return self._push(EditCommand(f"{scalar.name} = {value:g} {scalar.unit}", "scalar", scalar_id, before, value))

    # ------------------------------------------------------------------ history
    def _restore(self, cmd: EditCommand, use_after: bool) -> None:
        state = cmd.after if use_after else cmd.before
        if cmd.kind == "cells":
            table = self.calibration.table(cmd.target)
            table.values = state.copy()
            table.invalidate()
        elif cmd.kind == "axis":
            table = self.calibration.table(cmd.target)
            axis_values, values = state
            table.axes[cmd.extra["axis_index"]].values = list(axis_values)
            table.values = values.copy()
            table.invalidate()
        elif cmd.kind == "scalar":
            self.calibration.scalars[cmd.target].value = state

    @property
    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def undo(self) -> EditCommand:
        if not self.undo_stack:
            raise CalibrationError("Nothing to undo")
        cmd = self.undo_stack.pop()
        self._restore(cmd, use_after=False)
        self.redo_stack.append(cmd)
        return cmd

    def redo(self) -> EditCommand:
        if not self.redo_stack:
            raise CalibrationError("Nothing to redo")
        cmd = self.redo_stack.pop()
        self._restore(cmd, use_after=True)
        self.undo_stack.append(cmd)
        return cmd

    def history(self) -> dict:
        return {"undo": [c.to_dict() for c in self.undo_stack], "redo": [c.to_dict() for c in self.redo_stack]}


def _smooth(block: np.ndarray) -> np.ndarray:
    """Average each cell with its in-region neighbours (one pass)."""
    out = block.copy()
    for idx in np.ndindex(block.shape):
        acc, count = 0.0, 0
        for axis in range(block.ndim):
            for step in (-1, 1):
                j = list(idx)
                j[axis] += step
                if 0 <= j[axis] < block.shape[axis]:
                    acc += block[tuple(j)]
                    count += 1
        out[idx] = 0.5 * block[idx] + 0.5 * (acc / count if count else block[idx])
    return out


def _interpolate_corners(block: np.ndarray) -> np.ndarray:
    """Fill the region by multilinear interpolation between its corner cells."""
    out = np.empty_like(block)
    for idx in np.ndindex(block.shape):
        value = 0.0
        for corner in np.ndindex(*(2,) * block.ndim):
            weight = 1.0
            src = []
            for axis, c in enumerate(corner):
                n = block.shape[axis]
                t = idx[axis] / (n - 1) if n > 1 else 0.0
                weight *= t if c else 1.0 - t
                src.append(n - 1 if c else 0)
            value += weight * block[tuple(src)]
        out[idx] = value
    return out
