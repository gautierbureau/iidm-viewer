"""Allow ``python -m iidm_viewer.qt`` as an equivalent to the
``iidm-viewer-pyside`` console script.
"""
from __future__ import annotations

import sys

from iidm_viewer.qt.cli import main


if __name__ == "__main__":
    sys.exit(main())
