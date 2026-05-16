"""Console-script entry point for ``iidm-viewer-pyside``.

Usage::

    iidm-viewer-pyside [path/to/network.xiidm]

A positional path is optional. When provided, the app loads the file
immediately on startup; otherwise the user picks one via the sidebar's
"Load network…" button.
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="iidm-viewer-pyside",
        description="PySide6 desktop preview of iidm-viewer (Map + SLD tabs).",
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Optional network file to load on startup (.xiidm, .iidm, .xml, .zip, .mat, .uct).",
    )
    args = parser.parse_args()

    # Late import so ``--help`` doesn't pay the PySide6 import cost.
    from iidm_viewer.qt.main_window import run_app

    return run_app(initial_file=args.file)


if __name__ == "__main__":
    sys.exit(main())
