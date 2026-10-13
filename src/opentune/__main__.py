"""Allow ``python -m opentune``."""

from opentune.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
