"""Routers for the flash, control-lab, optimisation, AI, test-laboratory and tuning-engine modules."""

from __future__ import annotations

from fastapi import APIRouter


def routers() -> list[APIRouter]:
    from opentune.lab.api.routes import flash, labtools, tuning

    return [flash.router, labtools.router, tuning.router]
