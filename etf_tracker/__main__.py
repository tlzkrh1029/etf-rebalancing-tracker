"""``python3 -m etf_tracker`` -> :func:`etf_tracker.cli.main`."""

from __future__ import annotations

import sys

from etf_tracker.cli import main

if __name__ == "__main__":
    sys.exit(main())
