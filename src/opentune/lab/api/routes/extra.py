"""Routers for the flash, control-lab, optimisation, AI and test-laboratory modules."""

from __future__ import annotations

from fastapi import APIRouter


def routers() -> list[APIRouter]:
    from opentune.lab.api.routes import flash

    return [flash.router]
