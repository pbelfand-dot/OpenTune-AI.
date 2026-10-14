"""Virtual ECU device: simulated flash memory with A/B calibration banks and a bootloader.

Memory model (fictional, chosen to exercise realistic failure modes):

* Two calibration banks of ``BANK_SIZE`` bytes. Erased flash reads 0xFF and a
  write may only program erased bytes (like NOR flash), so writing over old
  data without an erase is an error.
* A boot control block (BCB) stored as two redundant copies, each
  ``seq u32 | bank u8 | CRC-32``. Activation writes the *older* copy with a
  higher sequence number, so losing power during activation leaves the other
  copy intact and the ECU boots the previous bank.
* On boot the bootloader picks the bank named by the newest valid BCB copy,
  checks its image (CRCs, SHA-256, firmware family, schema, engine id) and
  falls back to the other bank if it is not valid. With no valid bank it stays
  in bootloader (recovery) mode and the engine cannot run.

Fault hooks simulate power loss during erase/write/activation and a flash cell
that flips a bit after programming. Programming is refused while the engine is
running. The session handle returned by START_SESSION is a random value: it is
not a model of any security-access algorithm.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
from dataclasses import dataclass
from datetime import datetime, timezone

from opentune.ecu.safety import register_simulated_target_type
from opentune.lab.calibration.tables import FIRMWARE_FAMILY, SCHEMA_VERSION
from opentune.lab.flash import link as L
from opentune.lab.flash.image import ImageError, crc32, image_length, parse_header, verify_image

BANK_SIZE = 64 * 1024
BANKS = ("A", "B")
MAX_READ = 4096
FIRMWARE = {"family": FIRMWARE_FAMILY, "app_version": "1.4.0", "boot_version": "1.0.2",
            "hardware": "OTVECU-SIM (virtual)", "supported_schemas": [SCHEMA_VERSION]}


class PowerLoss(RuntimeError):
    pass


@dataclass
class DeviceFaults:
    power_loss_stage: str | None = None  # "erase" | "write" | "activate"
    power_loss_block: int = 0  # for "write": block index at which power is lost
    bitflip_block: int | None = None  # flash cell flips a bit after this block is written


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bcb(seq: int, bank: str) -> bytes:
    body = struct.pack(">IB", seq, BANKS.index(bank))
    return body + struct.pack(">I", crc32(body))


def _parse_bcb(raw: bytes) -> tuple[int, str] | None:
    if len(raw) != 9:
        return None
    body, (crc,) = raw[:5], struct.unpack(">I", raw[5:])
    if crc != crc32(body):
        return None
    seq, idx = struct.unpack(">IB", body)
    if idx >= len(BANKS):
        return None
    return seq, BANKS[idx]


@register_simulated_target_type
class VirtualEcuDevice:
    """In-memory ECU flash simulator (registered with the write-safety gate as *simulated*)."""

    is_simulated = True

    def __init__(self, engine_id: str, serial: str | None = None) -> None:
        self.engine_id = engine_id
        self.serial = serial or os.urandom(4).hex().upper()
        self.banks: dict[str, bytearray] = {b: bytearray(b"\xff" * BANK_SIZE) for b in BANKS}
        self.bcb: list[bytes] = [b"\xff" * 9, b"\xff" * 9]
        self.powered = True
        self.mode = "bootloader"
        self.running_bank: str | None = None
        self.running_sha: str | None = None
        self.engine_running = False
        self.session = "default"
        self.token: str | None = None
        self.faults = DeviceFaults()
        self._last: tuple[int, int, bytes] | None = None  # (seq, cmd, response) for duplicate detection
        self._blocks_written = 0
        self.boot_log: list[dict] = []
        self.history: list[dict] = []

    # ------------------------------------------------------------------ factory programming
    def factory_program(self, image: bytes, bank: str = "A") -> None:
        """Production-line programming of the initial calibration (direct memory, no link)."""
        verify_image(image)
        self.banks[bank][:] = b"\xff" * BANK_SIZE
        self.banks[bank][: len(image)] = image
        self.bcb = [_bcb(1, bank), b"\xff" * 9]
        self.boot()
        self._log("factory_program", f"bank {bank}")

    # ------------------------------------------------------------------ bank / BCB inspection
    def bcb_state(self) -> dict:
        copies = [_parse_bcb(c) for c in self.bcb]
        valid = [c for c in copies if c is not None]
        newest = max(valid, key=lambda c: c[0]) if valid else None
        return {"copies": [{"seq": c[0], "bank": c[1]} if c else None for c in copies],
                "active_bank": newest[1] if newest else None, "seq": newest[0] if newest else None}

    def bank_status(self, bank: str) -> dict:
        mem = bytes(self.banks[bank])
        if mem.count(0xFF) == len(mem):
            return {"bank": bank, "state": "erased"}
        try:
            header = verify_image(mem)
        except ImageError as exc:
            return {"bank": bank, "state": "invalid", "reason": str(exc)}
        problems = self.compatibility_problems(header.firmware_family, header.schema_version, header.engine_id)
        return {"bank": bank, "state": "incompatible" if problems else "valid", "reason": "; ".join(problems),
                "header": header.to_dict(), "image_bytes": image_length(header)}

    def compatibility_problems(self, family: str, schema: int, engine_id: str) -> list[str]:
        problems = []
        if family != FIRMWARE["family"]:
            problems.append(f"firmware family {family!r} ≠ {FIRMWARE['family']!r}")
        if schema not in FIRMWARE["supported_schemas"]:
            problems.append(f"calibration schema {schema} not supported by firmware {FIRMWARE['app_version']}")
        if engine_id != self.engine_id:
            problems.append(f"engine {engine_id!r} ≠ ECU configuration {self.engine_id!r}")
        return problems

    # ------------------------------------------------------------------ boot
    def boot(self) -> dict:
        self.powered = True
        self.session, self.token, self._last = "default", None, None
        bcb = self.bcb_state()
        order = [bcb["active_bank"]] if bcb["active_bank"] else []
        order += [b for b in BANKS if b not in order]
        report = {"time": _now(), "bcb_bank": bcb["active_bank"], "bcb_seq": bcb["seq"], "checked": []}
        for bank in order:
            st = self.bank_status(bank)
            report["checked"].append({"bank": bank, "state": st["state"], "reason": st.get("reason", "")})
            if st["state"] == "valid":
                self.mode, self.running_bank = "application", bank
                self.running_sha = st["header"]["payload_sha256"]
                report.update({"mode": "application", "bank": bank,
                               "fallback": bank != bcb["active_bank"]})
                break
        else:
            self.mode, self.running_bank, self.running_sha = "bootloader", None, None
            report.update({"mode": "bootloader", "bank": None, "fallback": False})
        self.boot_log.append(report)
        self.boot_log = self.boot_log[-50:]
        return report

    def power_off(self) -> None:
        self.powered = False
        self.session, self.token, self._last = "default", None, None

    def running_image(self) -> bytes | None:
        if self.running_bank is None:
            return None
        mem = bytes(self.banks[self.running_bank])
        return mem[: image_length(parse_header(mem))]

    # ------------------------------------------------------------------ protocol
    def handle(self, frame: bytes) -> bytes | None:
        if not self.powered:
            return None
        try:
            seq, cmd, payload = L.decode_frame(frame)
            header, data = L.decode_payload(payload)
        except L.FrameError:
            return L.encode_frame(0, 0xFF, L.encode_payload({"status": L.ST_BAD_FRAME}))
        if self._last is not None and self._last[:2] == (seq, cmd):
            return self._last[2]  # retransmitted request: replay the response, do not execute twice
        try:
            status, body, out = self._dispatch(cmd, header, data)
        except PowerLoss:
            return None
        resp = {"status": status, **body}
        if status != L.ST_OK:
            resp.setdefault("error", L.STATUS_TEXT.get(status, "error"))
        response = L.encode_frame(seq, cmd | 0x80, L.encode_payload(resp, out))
        if self.powered:
            self._last = (seq, cmd, response)
        return response

    def _need_programming(self, header: dict) -> int | None:
        if self.session != "programming":
            return L.ST_NOT_PROGRAMMING
        if header.get("token") != self.token:
            return L.ST_BAD_TOKEN
        return None

    def _dispatch(self, cmd: int, h: dict, data: bytes) -> tuple[int, dict, bytes]:
        if cmd == L.CMD_HELLO:
            return L.ST_OK, {"ident": {**FIRMWARE, "serial": self.serial, "engine_id": self.engine_id,
                                       "bank_size": BANK_SIZE, "banks": list(BANKS), "mode": self.mode,
                                       "simulated": True}}, b""
        if cmd == L.CMD_STATUS:
            return L.ST_OK, {"ecu": self.status()}, b""
        if cmd == L.CMD_START_SESSION:
            if h.get("mode") == "programming":
                if self.engine_running:
                    return L.ST_ENGINE_RUNNING, {}, b""
                self.session, self.token = "programming", os.urandom(8).hex()
                self._blocks_written = 0
                return L.ST_OK, {"session": "programming", "token": self.token}, b""
            self.session, self.token = "default", None
            return L.ST_OK, {"session": "default"}, b""
        if cmd == L.CMD_END_SESSION:
            self.session, self.token = "default", None
            return L.ST_OK, {"session": "default"}, b""
        bank = h.get("bank")
        if cmd in (L.CMD_READ, L.CMD_ERASE, L.CMD_WRITE, L.CMD_VERIFY, L.CMD_ACTIVATE) and bank not in BANKS:
            return L.ST_OUT_OF_RANGE, {}, b""
        if cmd == L.CMD_READ:
            off, n = int(h.get("offset", 0)), int(h.get("length", 0))
            if not (0 <= off and 0 < n <= MAX_READ and off + n <= BANK_SIZE):
                return L.ST_OUT_OF_RANGE, {}, b""
            return L.ST_OK, {}, bytes(self.banks[bank][off: off + n])
        if cmd == L.CMD_ERASE:
            if (err := self._need_programming(h)) is not None:
                return err, {}, b""
            if bank == self.running_bank:
                return L.ST_BANK_IN_USE, {}, b""
            if self.faults.power_loss_stage == "erase":
                self.banks[bank][: BANK_SIZE // 2] = b"\xff" * (BANK_SIZE // 2)
                self._lose_power("erase")
            self.banks[bank][:] = b"\xff" * BANK_SIZE
            self._log("erase", f"bank {bank}")
            return L.ST_OK, {}, b""
        if cmd == L.CMD_WRITE:
            if (err := self._need_programming(h)) is not None:
                return err, {}, b""
            if bank == self.running_bank:
                return L.ST_BANK_IN_USE, {}, b""
            off = int(h.get("offset", -1))
            if not (0 <= off and len(data) > 0 and off + len(data) <= BANK_SIZE):
                return L.ST_OUT_OF_RANGE, {}, b""
            region = self.banks[bank][off: off + len(data)]
            if region.count(0xFF) != len(region):
                return L.ST_NOT_ERASED, {}, b""
            block = self._blocks_written
            if self.faults.power_loss_stage == "write" and block == self.faults.power_loss_block:
                half = len(data) // 2
                self.banks[bank][off: off + half] = data[:half]
                self._lose_power(f"write block {block}")
            self.banks[bank][off: off + len(data)] = data
            if self.faults.bitflip_block is not None and block == self.faults.bitflip_block:
                self.banks[bank][off + len(data) // 2] ^= 0x04  # weak cell: one bit drops after programming
                self.faults.bitflip_block = None  # one-shot, like the other injected faults
            self._blocks_written += 1
            return L.ST_OK, {"written": len(data)}, b""
        if cmd == L.CMD_VERIFY:
            n = int(h.get("length", 0))
            if not 0 < n <= BANK_SIZE:
                return L.ST_OUT_OF_RANGE, {}, b""
            computed = hashlib.sha256(bytes(self.banks[bank][:n])).hexdigest()
            ok = computed == h.get("sha256")
            try:
                verify_image(bytes(self.banks[bank]))
                image_ok, reason = True, ""
            except ImageError as exc:
                image_ok, reason = False, str(exc)
            status = L.ST_OK if ok and image_ok else L.ST_VERIFY_FAILED
            return status, {"sha256": computed, "match": ok, "image_ok": image_ok, "reason": reason}, b""
        if cmd == L.CMD_ACTIVATE:
            if (err := self._need_programming(h)) is not None:
                return err, {}, b""
            st = self.bank_status(bank)
            if st["state"] != "valid":
                code = L.ST_INCOMPATIBLE if st["state"] == "incompatible" else L.ST_VERIFY_FAILED
                return code, {"reason": st.get("reason", st["state"])}, b""
            info = self.bcb_state()
            seq = (info["seq"] or 0) + 1
            parsed = [_parse_bcb(c) for c in self.bcb]
            # overwrite the invalid or older copy, never the newest valid one
            slot = 0 if parsed[0] is None else 1 if parsed[1] is None else (0 if parsed[0][0] < parsed[1][0] else 1)
            if self.faults.power_loss_stage == "activate":
                self.bcb[slot] = _bcb(seq, bank)[:5] + b"\x00\x00\x00\x00"  # torn write: bad CRC
                self._lose_power("activate")
            self.bcb[slot] = _bcb(seq, bank)
            self._log("activate", f"bank {bank} (BCB seq {seq}, slot {slot})")
            return L.ST_OK, {"bank": bank, "seq": seq}, b""
        if cmd == L.CMD_RESET:
            report = self.boot()
            return L.ST_OK, {"boot": report}, b""
        return L.ST_UNKNOWN_COMMAND, {}, b""

    def _lose_power(self, where: str) -> None:
        self.powered = False
        self.session, self.token, self._last = "default", None, None
        self.faults = DeviceFaults()  # one-shot
        self._log("power_loss", where)
        raise PowerLoss(where)

    def _log(self, op: str, detail: str) -> None:
        self.history.append({"time": _now(), "op": op, "detail": detail})
        self.history = self.history[-200:]

    # ------------------------------------------------------------------ status / persistence
    def status(self) -> dict:
        return {"mode": self.mode if self.powered else "off", "powered": self.powered,
                "running_bank": self.running_bank, "running_sha256": self.running_sha, "session": self.session,
                "engine_running": self.engine_running, "bcb": self.bcb_state(),
                "banks": {b: self.bank_status(b) for b in BANKS}, "firmware": dict(FIRMWARE),
                "serial": self.serial, "engine_id": self.engine_id, "simulated": True}

    def to_dict(self) -> dict:
        return {"engine_id": self.engine_id, "serial": self.serial,
                "banks": {b: base64.b64encode(bytes(m)).decode() for b, m in self.banks.items()},
                "bcb": [base64.b64encode(c).decode() for c in self.bcb], "powered": self.powered,
                "boot_log": self.boot_log, "history": self.history}

    @classmethod
    def from_dict(cls, d: dict) -> VirtualEcuDevice:
        dev = cls(d["engine_id"], d.get("serial"))
        for b in BANKS:
            raw = base64.b64decode(d["banks"][b])
            if len(raw) != BANK_SIZE:
                raise ValueError("Corrupt device file (bank size)")
            dev.banks[b][:] = raw
        dev.bcb = [base64.b64decode(c) for c in d["bcb"]]
        dev.boot_log = list(d.get("boot_log", []))
        dev.history = list(d.get("history", []))
        if d.get("powered", True):
            dev.boot()
        else:
            dev.powered = False
            dev.mode = "off"
        return dev
