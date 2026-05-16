"""Allow ``python -m iidm_viewer.web`` as an equivalent to the
``iidm-viewer-nicegui`` console script.
"""
from __future__ import annotations

import sys

from iidm_viewer.web.cli import main


if __name__ == "__main__":
    sys.exit(main())
