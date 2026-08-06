#!/usr/bin/env python3
"""Static server for the viewer, with HTTP Range support.

PMTiles works by reading byte ranges out of one large file, so the server has to
answer with 206 Partial Content. Python's own ``http.server`` does not implement
Range at all -- it replies 200 with the whole file, which makes the viewer fetch
all 24 MB for every tile it wants. That looks like "slow" rather than "broken",
which is the annoying way to discover it.

The pipeline writes its artefacts to ``data/out`` and the page lives in ``web``.
Rather than making you copy a 24 MB archive between the two every rebuild, the
files the pipeline emits are served from where it put them.

    python3 scripts/serve.py [--port 8099] [--dir web] [--out data/out]
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import re
import socketserver
from pathlib import Path

RANGE = re.compile(r"bytes=(\d*)-(\d*)")

# Emitted by `wayfare publish` into the output directory, not into web/. Any
# archive there is servable, not a fixed pair of names, so a machine holding
# several regions can offer all of them -- `wales.pmtiles` beside
# `london.pmtiles` -- and the viewer picks between them with ?tiles=.
ARTEFACT_SUFFIXES = (".pmtiles",)


def archives(out_dir: Path) -> list[str]:
    return sorted(p.name for p in out_dir.glob("*.pmtiles")) if out_dir.is_dir() else []


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    out_dir: Path = Path("data/out")

    def translate_path(self, path: str) -> str:
        """Resolve the pipeline's own outputs out of the artefact directory.

        Anything else is served from the page directory as usual, so the viewer
        stays a plain static bundle.
        """
        name = path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        # A bare name only: no directory part, so a request cannot climb out of the
        # artefact directory with "../".
        if name.endswith(ARTEFACT_SUFFIXES) and "/" not in name:
            candidate = self.out_dir / name
            if candidate.exists():
                return str(candidate)
        return super().translate_path(path)

    def do_GET(self) -> None:
        """Answer /archives.json so the page can offer whatever regions are built.

        The viewer is a static bundle and cannot list a directory, so without this
        it would need the region names compiled into it -- and a machine that had
        just built a new one would not show it.
        """
        if self.path.split("?", 1)[0] == "/archives.json":
            body = json.dumps(archives(self.out_dir)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def send_head(self):  # type: ignore[no-untyped-def]
        header = self.headers.get("Range")
        if not header:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")  # noqa: SIM115 - closed by the caller, as in the base class
        except OSError:
            self.send_error(404)
            return None

        size = os.fstat(f.fileno()).st_size
        m = RANGE.fullmatch(header.strip())
        if not m:
            f.close()
            self.send_error(400, "malformed Range")
            return None

        first, last = m.group(1), m.group(2)
        if first:
            start = int(first)
            end = int(last) if last else size - 1
        else:
            # A suffix range -- "the last N bytes". PMTiles uses this to find the
            # footer without knowing the file length up front.
            start = max(0, size - int(last))
            end = size - 1
        end = min(end, size - 1)

        if start >= size or start > end:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self._cors()
        self.end_headers()
        f.seek(start)
        return _Slice(f, end - start + 1)

    def end_headers(self) -> None:
        if self.command == "OPTIONS" or "Content-Range" not in self._headers_buffer_str():
            self._cors()
        super().end_headers()

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges")

    def _headers_buffer_str(self) -> str:
        return b"".join(getattr(self, "_headers_buffer", [])).decode("latin-1")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # one line per tile is unreadable


class _Slice:
    """A file object that stops after n bytes, so copyfile sends only the range."""

    def __init__(self, f, n: int):  # type: ignore[no-untyped-def]
        self.f = f
        self.left = n

    def read(self, size: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        if size < 0 or size > self.left:
            size = self.left
        data = self.f.read(size)
        self.left -= len(data)
        return data

    def close(self) -> None:
        self.f.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--dir", type=Path, default=Path("web"))
    ap.add_argument("--out", type=Path, default=Path("data/out"))
    args = ap.parse_args()

    RangeHandler.out_dir = args.out.resolve()
    found = archives(RangeHandler.out_dir)
    if found:
        for name in found:
            size = (RangeHandler.out_dir / name).stat().st_size / 1e6
            print(f"tiles: {name} ({size:.1f} MB)")
    else:
        print(f"no .pmtiles in {args.out} -- run `wayfare publish` first")

    handler = functools.partial(RangeHandler, directory=str(args.dir.resolve()))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", args.port), handler) as httpd:
        print(f"serving {args.dir} at http://localhost:{args.port}/  (ctrl-c to stop)")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
