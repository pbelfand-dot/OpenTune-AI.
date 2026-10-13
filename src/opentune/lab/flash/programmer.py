"""Host-side programmer for the virtual ECU: checks, backup, erase/write/verify, activation, rollback.

Every operation that changes ECU memory first passes
:func:`opentune.ecu.safety.guard_write`, and the programmer refuses any link
that is not the in-process :class:`LoopbackLink` to a :class:`VirtualEcuDevice`.
So this code cannot program physical hardware: there is no transport for it.

All reports are labelled *simulated*.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from opentune.ecu.safety import EcuWriteBlockedError, guard_write
from opentune.lab.calibration.tables import Calibration
from opentune.lab.flash import link as L
from opentune.lab.flash.device import BANK_SIZE, BANKS, MAX_READ, VirtualEcuDevice
from opentune.lab.flash.image import (
    HEADER_SIZE,
    ImageError,
    decode_image,
    image_length,
    parse_header,
    verify_image,
)


class FlashError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class FlashCancelled(FlashError):
    pass


@dataclass
class Step:
    step: str
    status: str  # ok | failed | skipped | warning
    detail: str = ""
    t_ms: float = 0.0


@dataclass
class FlashReport:
    operation: str
    success: bool = False
    steps: list[Step] = field(default_factory=list)
    target_bank: str | None = None
    previous_bank: str | None = None
    image_sha256: str | None = None
    image_bytes: int = 0
    backup: bytes | None = None
    boot: dict | None = None
    link: dict = field(default_factory=dict)
    retries: int = 0
    error: str | None = None
    ecu_state_after: dict | None = None
    advice: list[str] = field(default_factory=list)
    simulated: bool = True
    wall_seconds: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["backup"] = {"bytes": len(self.backup), "sha256": hashlib.sha256(self.backup).hexdigest()} \
            if self.backup else None
        d["label"] = "SIMULATED flash of the OpenTune virtual ECU — no physical hardware involved"
        return d


Progress = Callable[[float, str], None]


class Programmer:
    def __init__(self, link: L.LoopbackLink, block_size: int = 512, retries: int = 3,
                 progress: Progress | None = None, cancel: Callable[[], bool] | None = None) -> None:
        # Hard type checks (not duck typing): only the in-process loopback to a virtual device.
        if type(link) is not L.LoopbackLink or type(link.device) is not VirtualEcuDevice:
            raise EcuWriteBlockedError("The programmer only talks to the in-process virtual ECU")
        if not 64 <= block_size <= 2048 or block_size % 16:
            raise ValueError("block_size must be a multiple of 16 between 64 and 2048")
        self.link = link
        self.device = link.device
        self.block_size = block_size
        self.max_retries = retries
        self.progress = progress or (lambda f, m: None)
        self.cancel = cancel or (lambda: False)
        self._seq = 0
        self.retries = 0
        self._t0 = time.perf_counter()

    # ------------------------------------------------------------------ transport
    def request(self, cmd: int, header: dict | None = None, data: bytes = b"") -> tuple[dict, bytes]:
        last: Exception | None = None
        # One sequence number per request; retransmissions reuse it so the ECU can recognise a
        # duplicate (lost/corrupted response) and replay its answer instead of executing twice.
        self._seq = (self._seq + 1) & 0xFF
        frame = L.encode_frame(self._seq, cmd, L.encode_payload(header or {}, data))
        for _attempt in range(self.max_retries + 1):
            try:
                raw = self.link.transfer(frame)
                seq, rcmd, payload = L.decode_frame(raw)
                resp, out = L.decode_payload(payload)
            except L.LinkDown:
                raise
            except (L.LinkTimeout, L.FrameError) as exc:
                last = exc
                self.retries += 1
                continue
            if resp.get("status") == L.ST_BAD_FRAME:
                last = FlashError("ECU reported a corrupted frame", L.ST_BAD_FRAME)
                self.retries += 1
                continue
            if rcmd != (cmd | 0x80) or seq != self._seq:
                last = FlashError("response does not match request")
                self.retries += 1
                continue
            if resp.get("status") != L.ST_OK:
                raise FlashError(f"{L.COMMAND_NAMES.get(cmd, hex(cmd))}: "
                                 f"{resp.get('error', 'error')} {resp.get('reason', '')}".strip(), resp.get("status"))
            return resp, out
        raise FlashError(f"{L.COMMAND_NAMES.get(cmd, hex(cmd))}: no valid response after "
                         f"{self.max_retries + 1} attempts ({last})")

    def _ms(self) -> float:
        return round(self.link.sim_time * 1000.0, 2)

    # ------------------------------------------------------------------ read-only operations
    def identify(self) -> dict:
        return self.request(L.CMD_HELLO)[0]["ident"]

    def status(self) -> dict:
        return self.request(L.CMD_STATUS)[0]["ecu"]

    def read_bank(self, bank: str, length: int | None = None) -> bytes:
        if bank not in BANKS:
            raise FlashError("unknown bank")
        if length is None:
            head = self.request(L.CMD_READ, {"bank": bank, "offset": 0, "length": HEADER_SIZE})[1]
            length = image_length(parse_header(head))
        if not 0 < length <= BANK_SIZE:
            raise FlashError("bad length")
        out = bytearray()
        while len(out) < length:
            n = min(MAX_READ, length - len(out))
            out += self.request(L.CMD_READ, {"bank": bank, "offset": len(out), "length": n})[1]
        return bytes(out)

    def read_calibration(self) -> Calibration:
        st = self.status()
        if st["running_bank"] is None:
            raise FlashError("ECU is in bootloader mode — no running calibration to read")
        return decode_image(self.read_bank(st["running_bank"]))

    # ------------------------------------------------------------------ checks
    def compatibility(self, image: bytes, ident: dict) -> list[Step]:
        checks: list[Step] = []
        try:
            header = verify_image(image)
            checks.append(Step("image_integrity", "ok", "header CRC, image CRC and payload SHA-256 verified"))
        except ImageError as exc:
            return [Step("image_integrity", "failed", str(exc))]
        checks.append(Step("firmware_family", "ok" if header.firmware_family == ident["family"] else "failed",
                           f"image {header.firmware_family!r}, ECU {ident['family']!r}"))
        checks.append(Step("schema_version", "ok" if header.schema_version in ident["supported_schemas"] else "failed",
                           f"image schema {header.schema_version}, firmware {ident['app_version']} supports "
                           f"{ident['supported_schemas']}"))
        checks.append(Step("engine_match", "ok" if header.engine_id == ident["engine_id"] else "failed",
                           f"image {header.engine_id!r}, ECU {ident['engine_id']!r}"))
        size = image_length(header)
        checks.append(Step("size", "ok" if size <= ident["bank_size"] else "failed",
                           f"{size} bytes, bank {ident['bank_size']} bytes"))
        return checks

    # ------------------------------------------------------------------ programming
    def flash(self, image: bytes, operation: str = "flash") -> FlashReport:
        report = FlashReport(operation)
        t_wall = time.perf_counter()

        def step(name: str, status: str, detail: str = "") -> None:
            report.steps.append(Step(name, status, detail, self._ms()))

        target = None
        try:
            self.progress(0.01, "connect")
            ident = self.identify()
            step("connect", "ok", f"{ident['hardware']} serial {ident['serial']}, firmware {ident['app_version']}, "
                                  f"bootloader {ident['boot_version']}")
            st = self.status()
            report.previous_bank = st["running_bank"]
            step("read_status", "ok", f"mode {st['mode']}, running bank {st['running_bank']}")
            checks = self.compatibility(image, ident)
            report.steps.extend(checks)
            if any(c.status == "failed" for c in checks):
                raise FlashError("pre-flash checks failed — nothing was written")
            header = parse_header(image)
            report.image_sha256, report.image_bytes = header.payload_sha256, len(image)
            if st["engine_running"]:
                step("precondition", "failed", "engine running")
                raise FlashError("Engine running — switch the engine off (ignition on) before programming")
            step("precondition", "ok", "engine off, ECU powered")
            if st["running_bank"] is not None:
                self.progress(0.05, "backup")
                backup = self.read_bank(st["running_bank"])
                verify_image(backup)
                report.backup = backup
                step("backup", "ok", f"bank {st['running_bank']}: {len(backup)} bytes read back and verified")
            else:
                step("backup", "skipped", "no valid running calibration (bootloader mode)")
            target = next(b for b in BANKS if b != st["running_bank"])
            report.target_bank = target
            guard_write(self.device)
            token = self.request(L.CMD_START_SESSION, {"mode": "programming"})[0]["token"]
            step("programming_session", "ok", "session opened (random session handle; no security algorithm "
                                              "is modelled)")
            self.progress(0.12, "erase")
            self.request(L.CMD_ERASE, {"bank": target, "token": token})
            step("erase", "ok", f"bank {target} erased")
            blocks = [image[i: i + self.block_size] for i in range(0, len(image), self.block_size)]
            for n, block in enumerate(blocks):
                if self.cancel():
                    raise FlashCancelled("cancelled by user during write")
                self.request(L.CMD_WRITE, {"bank": target, "offset": n * self.block_size, "token": token}, block)
                self.progress(0.12 + 0.7 * (n + 1) / len(blocks), f"write {n + 1}/{len(blocks)}")
            step("write", "ok", f"{len(blocks)} blocks × {self.block_size} bytes to bank {target}")
            self.progress(0.85, "verify")
            v = self.request(L.CMD_VERIFY, {"bank": target, "length": len(image),
                                            "sha256": hashlib.sha256(image).hexdigest()})[0]
            step("verify", "ok", f"ECU-computed SHA-256 matches ({v['sha256'][:16]}…), image checks passed")
            self.progress(0.9, "activate")
            a = self.request(L.CMD_ACTIVATE, {"bank": target, "token": token})[0]
            step("activate", "ok", f"boot control block → bank {target} (seq {a['seq']})")
            self.progress(0.95, "reset")
            boot = self.request(L.CMD_RESET)[0]["boot"]
            report.boot = boot
            if boot.get("bank") != target:
                step("reset", "failed", f"ECU booted bank {boot.get('bank')} instead of {target}")
                raise FlashError("post-reset check failed")
            step("reset", "ok", f"ECU booted bank {target} in {boot['mode']} mode")
            if self.status()["running_sha256"] != header.payload_sha256:
                raise FlashError("running calibration hash differs from the image")
            step("post_check", "ok", "running calibration hash equals the flashed image")
            report.success = True
        except FlashCancelled as exc:
            report.error = str(exc)
            step("abort", "failed", str(exc))
            self._recover_after_error(report)
        except (FlashError, L.LinkError, ImageError, EcuWriteBlockedError) as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            step("abort", "failed", str(exc))
            self._recover_after_error(report)
        report.retries = self.retries
        report.link = self.link.stats()
        report.ecu_state_after = self.device.status()
        report.wall_seconds = round(time.perf_counter() - t_wall, 4)
        self.progress(1.0, "done" if report.success else "failed")
        return report

    def _recover_after_error(self, report: FlashReport) -> None:
        dev = self.device
        if not dev.powered:
            report.advice.append("The ECU lost power during programming (simulated). After a power cycle the "
                                 "bootloader picks the newest valid bank; the previous calibration is intact.")
        elif self.link.down:
            report.advice.append("The link dropped mid-transfer. The partially written bank is not activated; the "
                                 "ECU keeps running the previous calibration. Reconnect and flash again.")
        else:
            try:
                self.request(L.CMD_END_SESSION)
            except (FlashError, L.LinkError):
                pass
        if report.target_bank and dev.powered:
            st = dev.bank_status(report.target_bank)
            report.advice.append(f"Bank {report.target_bank} is now '{st['state']}'"
                                 + (f" ({st['reason']})" if st.get("reason") else "") + "; it is not used for boot.")
        if report.previous_bank:
            report.advice.append(f"Backup of bank {report.previous_bank} is available for restore.")

    def rollback(self) -> FlashReport:
        """Switch back to the other bank if it holds a valid calibration (no data is written)."""
        report = FlashReport("rollback")
        try:
            st = self.status()
            current = st["running_bank"]
            other = next((b for b in BANKS if b != current), None)
            report.previous_bank, report.target_bank = current, other
            if other is None or st["banks"][other]["state"] != "valid":
                raise FlashError(f"No valid calibration in bank {other} to roll back to")
            if st["engine_running"]:
                raise FlashError("Engine running — switch the engine off first")
            guard_write(self.device)
            token = self.request(L.CMD_START_SESSION, {"mode": "programming"})[0]["token"]
            a = self.request(L.CMD_ACTIVATE, {"bank": other, "token": token})[0]
            report.steps.append(Step("activate", "ok", f"boot control block → bank {other} (seq {a['seq']})",
                                     self._ms()))
            boot = self.request(L.CMD_RESET)[0]["boot"]
            report.boot = boot
            report.success = boot.get("bank") == other
            report.steps.append(Step("reset", "ok" if report.success else "failed",
                                     f"ECU booted bank {boot.get('bank')}", self._ms()))
            report.image_sha256 = boot and self.device.running_sha
        except (FlashError, L.LinkError, EcuWriteBlockedError) as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            report.steps.append(Step("abort", "failed", str(exc), self._ms()))
        report.link = self.link.stats()
        report.ecu_state_after = self.device.status()
        return report
