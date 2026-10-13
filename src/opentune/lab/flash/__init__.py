"""Simulated ECU communication and flashing (virtual ECU only).

``image``      OTCI fictional calibration image format (CRC-32 + SHA-256)
``link``       OTVL fictional frame protocol + in-process loopback transport
``device``     virtual ECU flash memory: A/B banks, redundant boot control, bootloader
``programmer`` host procedures: checks, backup, erase/write/verify, activate, rollback

Nothing in this package can reach physical hardware.
"""

from opentune.lab.flash.device import BANK_SIZE, BANKS, DeviceFaults, VirtualEcuDevice
from opentune.lab.flash.image import ImageError, build_image, decode_image, verify_image
from opentune.lab.flash.link import LinkFaults, LoopbackLink
from opentune.lab.flash.programmer import FlashError, FlashReport, Programmer

__all__ = [
    "BANKS",
    "BANK_SIZE",
    "DeviceFaults",
    "FlashError",
    "FlashReport",
    "ImageError",
    "LinkFaults",
    "LoopbackLink",
    "Programmer",
    "VirtualEcuDevice",
    "build_image",
    "decode_image",
    "verify_image",
]
