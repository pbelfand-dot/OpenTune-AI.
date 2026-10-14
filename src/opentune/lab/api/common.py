"""Response helpers shared by the API routers."""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
from fastapi import Request
from fastapi.responses import JSONResponse


def jsonable(obj: Any) -> Any:
    """Recursively convert to JSON-safe values (NaN/inf → None, numpy → Python)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return jsonable(obj.tolist())
    if isinstance(obj, np.generic):
        return jsonable(obj.item())
    return obj


class SafeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(jsonable(content), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def state(request: Request):
    return request.app.state.lab
