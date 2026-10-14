"""Static and runtime checks: the lab cannot send write operations to physical automotive hardware.

These tests scan the lab source tree for hardware-access libraries and make sure
the only network client (the optional rusEFI simulator integration) is
loopback-only and read-only.
"""

from __future__ import annotations

import ast
from pathlib import Path

import opentune.lab as lab_pkg
from opentune.ecu.safety import REAL_ECU_WRITE_ENABLED

LAB_ROOT = Path(lab_pkg.__file__).resolve().parent

# Modules that talk to serial ports, CAN, USB, J2534/PassThru or vendor interfaces.
FORBIDDEN_IMPORTS = {"serial", "can", "usb", "pyusb", "ftd2xx", "pyftdi", "j2534", "pyj2534", "udsoncan", "isotp",
                     "python_can", "canlib", "pcan", "kvaser", "panda", "obd", "elm327"}
FORBIDDEN_TEXT = ("AF_CAN", "PF_CAN", "CAN_RAW", "/dev/tty", "COM1", "PassThruOpen", "WinDLL", "cdll.LoadLibrary",
                  "ctypes.CDLL")
# Files allowed to open sockets, and what they must contain to prove they are loopback/read-only.
SOCKET_ALLOWED = {"integrations/rusefi.py"}


def _py_files():
    return [p for p in LAB_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _imports(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_real_ecu_writes_disabled_constant():
    assert REAL_ECU_WRITE_ENABLED is False


def test_lab_imports_no_hardware_libraries():
    offenders = {}
    for path in _py_files():
        bad = _imports(ast.parse(path.read_text(encoding="utf-8"))) & FORBIDDEN_IMPORTS
        if bad:
            offenders[str(path.relative_to(LAB_ROOT))] = sorted(bad)
    assert offenders == {}


def test_lab_has_no_raw_hardware_access_strings():
    hits = []
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        hits += [f"{path.relative_to(LAB_ROOT)}: {s}" for s in FORBIDDEN_TEXT if s in text]
    assert hits == []


def test_sockets_only_in_the_loopback_simulator_client():
    users = set()
    for path in _py_files():
        rel = str(path.relative_to(LAB_ROOT))
        if "socket" in _imports(ast.parse(path.read_text(encoding="utf-8"))):
            users.add(rel)
    assert users <= SOCKET_ALLOWED, users - SOCKET_ALLOWED


def test_flash_package_has_only_the_loopback_transport():
    from opentune.lab.flash import link

    transports = [n for n in dir(link) if n.endswith("Link") and isinstance(getattr(link, n), type)]
    assert transports == ["LoopbackLink"]


def test_api_exposes_no_hardware_endpoints():
    import tempfile

    from opentune.lab.api.app import create_app

    app = create_app(tempfile.mkdtemp(), demo_projects=False)
    paths = " ".join(getattr(r, "path", "") for r in app.routes).lower()
    for word in ("serial", "j2534", "passthru", "/can", "usb", "elm327", "obd", "vin"):
        assert word not in paths, word
