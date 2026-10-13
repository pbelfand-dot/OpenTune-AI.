"""Application paths and persisted user settings.

Data lives in the platform's per-user application data directory and can be
overridden with the ``OPENTUNE_DATA_DIR`` environment variable (used by tests
and by the cloud simulation mode).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_DIR_NAME = "OpenTune AI"
ENV_DATA_DIR = "OPENTUNE_DATA_DIR"


def default_data_dir() -> Path:
    """Return the per-user data directory for the current platform."""
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_DIR_NAME
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        return (Path(base) if base else home / "AppData" / "Roaming") / APP_DIR_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / "opentune"


@dataclass
class AppPaths:
    """Resolved directories used by the application."""

    root: Path

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def calibrations(self) -> Path:
        return self.root / "calibrations"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    def ensure(self) -> AppPaths:
        for path in (self.root, self.logs, self.calibrations, self.reports):
            path.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def default(cls) -> AppPaths:
        return cls(default_data_dir()).ensure()


@dataclass
class Settings:
    """User preferences persisted as JSON."""

    engine: str = "B58"
    unit_system: str = "imperial"  # "imperial" or "metric"
    connection: str = "simulator"  # "simulator", "simulator-obd", "elm327"
    serial_port: str = ""
    serial_baudrate: int = 38400
    sample_rate_hz: float = 20.0
    local_model_enabled: bool = False
    local_model_backend: str = "ollama"  # "ollama" or "openai-compatible"
    local_model_url: str = "http://127.0.0.1:11434"
    local_model_name: str = "llama3.2"
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Settings:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)
