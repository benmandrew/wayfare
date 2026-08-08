#!/usr/bin/env python3
"""Deprecated shim. The server moved into the package as `wayfare serve`.

It moved so that it is covered by mypy and the test suite -- pyproject checks
`wayfare` and nothing else, and the server now renders on request rather than only
copying bytes, which is not code to leave unchecked.

This file stays because a running deployment's compose file may still name it.

    wayfare serve --dir web --out data/out --port 8099
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wayfare import logs, server


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--dir", type=Path, default=Path("web"))
    ap.add_argument("--out", type=Path, default=Path("data/out"))
    args = ap.parse_args()

    logs.setup(None)
    print(
        "scripts/serve.py is deprecated; run `wayfare serve` instead",
        file=sys.stderr,
    )
    server.serve(port=args.port, web_dir=args.dir, out_dir=args.out)


if __name__ == "__main__":
    main()
