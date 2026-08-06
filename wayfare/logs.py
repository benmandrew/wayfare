"""Logging set up for a run that lasts days on a headless server.

Timestamps are absolute and UTC, because the useful question after the fact is
always "what was happening at 03:00", not "how long had it been running".
"""

from __future__ import annotations

import logging
import os
import sys

FORMAT = "%(asctime)s %(levelname)-7s %(name)-14s %(message)s"
DATEFMT = "%Y-%m-%dT%H:%M:%SZ"


def setup(level: str | None = None) -> None:
    logging.Formatter.converter = __import__("time").gmtime
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level or os.environ.get("WAYFARE_LOG", "INFO").upper())
    # urllib3 logs a line per connection; at six workers for two days that is noise.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"wayfare.{name}")
