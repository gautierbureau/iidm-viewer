"""Console-script entry point for ``iidm-viewer-nicegui``.

Usage::

    iidm-viewer-nicegui [path/to/network.xiidm]
    iidm-viewer-nicegui --no-native --port 8669

A positional path is optional. ``--no-native`` runs as a plain
localhost server instead of opening a pywebview desktop window —
useful for testing without GUI libs installed.
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="iidm-viewer-nicegui",
        description="NiceGUI preview of iidm-viewer (Map + SLD tabs).",
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Optional network file to load on startup.",
    )
    parser.add_argument(
        "--no-native",
        dest="native",
        action="store_false",
        default=True,
        help="Run as a localhost HTTP server instead of a pywebview window.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8669,
        help="Port for the NiceGUI server (default: 8669).",
    )
    args = parser.parse_args()

    # Late import so ``--help`` doesn't pay the NiceGUI import cost.
    from iidm_viewer.web.app import run_app

    run_app(initial_file=args.file, native=args.native, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
