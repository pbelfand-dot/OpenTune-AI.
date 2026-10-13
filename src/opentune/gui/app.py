"""Desktop application entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from opentune import __app_name__, __version__
from opentune.config import AppPaths
from opentune.gui.context import AppContext
from opentune.gui.main_window import MainWindow
from opentune.gui.theme import apply_theme


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opentune-gui")
    parser.add_argument("--data-dir", help="override the data directory")
    parser.add_argument("--page", default="dashboard", help="page to open on start")
    parser.add_argument("--scenario", default="city", help="simulator driving scenario on start")
    parser.add_argument("--screenshot", help="save a PNG of the window after --delay ms and exit")
    parser.add_argument("--delay", type=int, default=3000)
    args, qt_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    app = QApplication.instance() or QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("OpenTune")
    apply_theme(app)

    paths = AppPaths(Path(args.data_dir)).ensure() if args.data_dir else AppPaths.default()
    ctx = AppContext(paths)
    window = MainWindow(ctx, scenario=args.scenario)
    window.show_page(args.page)
    window.show()

    if args.screenshot:
        def snap() -> None:
            window.grab().save(args.screenshot)
            window.close()
            app.quit()

        QTimer.singleShot(args.delay, snap)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
