from __future__ import annotations

import zipfile
from pathlib import Path

import builders
import pytest

from wayfare import acquire, config


def _zip(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, "a,b\n1,2\n")
    return path


# --- Caching ----------------------------------------------------------------


def test_a_complete_file_is_never_refetched(tmp_path, monkeypatch):
    dest = tmp_path / "bods_gtfs_wales.zip"
    _zip(dest, list(acquire.REQUIRED_GTFS))

    def boom(*a, **k):
        raise AssertionError("must not hit the network when the file is present")

    monkeypatch.setattr(acquire, "_stream", boom)
    src = acquire.sources("wales")[0]
    assert acquire.download(src, tmp_path) == dest


def test_force_overrides_the_cache(tmp_path, monkeypatch):
    dest = tmp_path / "bods_gtfs_wales.zip"
    _zip(dest, list(acquire.REQUIRED_GTFS))
    calls = []

    def fake(src, part):
        calls.append(src.name)
        _zip(part, list(acquire.REQUIRED_GTFS))

    monkeypatch.setattr(acquire, "_stream", fake)
    monkeypatch.setattr(config, "MIN_GTFS_BYTES", 1)
    acquire.download(acquire.sources("wales")[0], tmp_path, force=True)
    assert calls == ["gtfs"]


def test_unpacked_feed_is_not_re_extracted(tmp_path, monkeypatch):
    z = _zip(tmp_path / "feed.zip", ["stop_times.txt", "trips.txt"])
    out = tmp_path / "gtfs"
    acquire.unpack_gtfs(z, out)
    monkeypatch.setattr(
        zipfile, "ZipFile", lambda *a, **k: pytest.fail("re-extracted a cached feed")
    )
    assert acquire.unpack_gtfs(z, out) == out


# --- Retry policy -----------------------------------------------------------


def test_a_bad_archive_is_not_retried(tmp_path, monkeypatch):
    """The expensive mistake this guards against: five full re-downloads of a
    file the server will hand back byte-identical every time."""
    attempts = []

    def fake(src, part):
        attempts.append(1)
        part.write_bytes(b"<html>error</html>" * 100_000)

    monkeypatch.setattr(acquire, "_stream", fake)
    monkeypatch.setattr(config, "MIN_GTFS_BYTES", 1)
    with pytest.raises(acquire.Invalid):
        acquire.download(acquire.sources("wales")[0], tmp_path)
    assert len(attempts) == 1


def test_a_short_file_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "_stream", lambda src, part: part.write_bytes(b"x"))
    with pytest.raises(acquire.Invalid, match="expected at least"):
        acquire.download(acquire.sources("wales")[0], tmp_path)


def test_network_failures_are_retried(tmp_path, monkeypatch):
    attempts = []

    def flaky(src, part):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("connection reset")
        _zip(part, list(acquire.REQUIRED_GTFS))

    monkeypatch.setattr(acquire, "_stream", flaky)
    monkeypatch.setattr(config, "DOWNLOAD_BACKOFF", 0.0)
    monkeypatch.setattr(config, "MIN_GTFS_BYTES", 1)
    acquire.download(acquire.sources("wales")[0], tmp_path)
    assert len(attempts) == 3


def test_partials_are_kept_only_when_the_host_can_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOAD_BACKOFF", 0.0)
    monkeypatch.setattr(config, "DOWNLOAD_RETRIES", 1)

    def die(src, part):
        part.write_bytes(b"partial")
        raise ConnectionError("dropped")

    monkeypatch.setattr(acquire, "_stream", die)

    # BODS ignores Range, so a partial is dead weight.
    with pytest.raises(RuntimeError):
        acquire.download(acquire.sources("wales")[0], tmp_path)
    assert not (tmp_path / "bods_gtfs_wales.zip.part").exists()

    # Geofabrik answers 206, so the bytes already paid for are kept.
    osm = [s for s in acquire.sources("wales", with_osm=True) if s.name == "osm"][0]
    with pytest.raises(RuntimeError):
        acquire.download(osm, tmp_path)
    assert (tmp_path / f"{osm.filename}.part").read_bytes() == b"partial"


# --- Archive validation -----------------------------------------------------


def test_truncated_zip_is_rejected(tmp_path):
    z = _zip(tmp_path / "f.zip", list(acquire.REQUIRED_GTFS))
    data = z.read_bytes()
    z.write_bytes(data[: len(data) // 2])  # loses the central directory
    with pytest.raises(OSError, match="truncated"):
        acquire.check_gtfs(z)


def test_missing_members_are_named(tmp_path):
    z = _zip(tmp_path / "f.zip", ["stops.txt", "routes.txt"])
    with pytest.raises(OSError, match="stop_times.txt"):
        acquire.check_gtfs(z)


# --- Resume -----------------------------------------------------------------


def response(status: int, body: bytes, headers: dict[str, str] | None = None):
    """`builders.FakeResponse` with the Content-Length every host but BODS sends.

    Declaring it by default is what keeps the completeness check in `_stream` live
    for the tests that are about something else, so a transfer cut short in one of
    them fails there rather than being written out short and passing.
    """
    return builders.FakeResponse(
        body, status, headers or {"Content-Length": str(len(body))}
    )


def test_resume_appends_on_206(tmp_path, monkeypatch):
    part = tmp_path / "x.pbf.part"
    part.write_bytes(b"AAAA")
    seen = {}

    def fake_get(url, headers=None, **k):
        seen.update(headers or {})
        return response(206, b"BBBB")

    monkeypatch.setattr(acquire.requests, "get", fake_get)
    src = acquire.Source("osm", "http://x/y.pbf", "x.pbf", resumable=True)
    acquire._stream(src, part)
    assert seen["Range"] == "bytes=4-"
    assert part.read_bytes() == b"AAAABBBB"


def test_a_server_that_ignores_range_restarts_cleanly(tmp_path, monkeypatch):
    """Appending a full-file 200 response onto a partial yields a file that is
    the right shape and the wrong bytes -- the worst kind of corruption."""
    part = tmp_path / "x.pbf.part"
    part.write_bytes(b"AAAA")
    monkeypatch.setattr(
        acquire.requests, "get", lambda url, headers=None, **k: response(200, b"WHOLE")
    )
    src = acquire.Source("osm", "http://x/y.pbf", "x.pbf", resumable=True)
    acquire._stream(src, part)
    assert part.read_bytes() == b"WHOLE"


# --- Declared length --------------------------------------------------------


def test_a_short_read_is_caught_where_the_host_declares_a_length(tmp_path, monkeypatch):
    """BODS sends no Content-Length, which is why check_gtfs opens the archive.
    Every other host does send one, and it turns a cut-short transfer into an error
    where it happened rather than a puzzle three stages later."""
    monkeypatch.setattr(
        acquire.requests,
        "get",
        lambda url, headers=None, **k: response(200, b"AB", {"Content-Length": "1000"}),
    )
    src = acquire.Source("gtfs", "http://x/f.zip", "f.zip")
    with pytest.raises(OSError, match="cut short"):
        acquire._stream(src, tmp_path / "f.zip.part")


def test_a_compressed_body_is_not_measured_against_the_declared_length(
    tmp_path, monkeypatch
):
    """requests decodes the body, so the bytes written are the decoded ones and the
    declared length describes something else entirely."""
    monkeypatch.setattr(
        acquire.requests,
        "get",
        lambda url, headers=None, **k: response(
            200, b"decoded and longer", {"Content-Length": "4", "Content-Encoding": "gzip"}
        ),
    )
    src = acquire.Source("naptan", "http://x/f.csv", "f.csv")
    acquire._stream(src, tmp_path / "f.csv.part")
    assert (tmp_path / "f.csv.part").read_bytes() == b"decoded and longer"


def test_non_resumable_sources_send_no_range_header(tmp_path, monkeypatch):
    part = tmp_path / "f.zip.part"
    part.write_bytes(b"AAAA")
    seen = {}

    def fake_get(url, headers=None, **k):
        seen.update(headers or {})
        return response(200, b"WHOLE")

    monkeypatch.setattr(acquire.requests, "get", fake_get)
    acquire._stream(acquire.sources("wales")[0], part)
    assert "Range" not in seen


# --- Credentials ------------------------------------------------------------
#
# Network Rail's SCHEDULE feed is the first source behind a login. What is tested
# here is mostly that a refusal stops immediately: a 401 retried five times over
# rising backoff proves nothing about the password and looks, from the far end,
# exactly like guessing at it.


def test_credentials_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("WAYFARE_NROD_USER", "someone@example.com")
    monkeypatch.setenv("WAYFARE_NROD_PASS", "hunter2")
    assert acquire.credentials("NROD") == ("someone@example.com", "hunter2")


def test_missing_credentials_name_the_variables_not_the_value(monkeypatch):
    monkeypatch.delenv("WAYFARE_NROD_USER", raising=False)
    monkeypatch.delenv("WAYFARE_NROD_PASS", raising=False)
    with pytest.raises(acquire.Unauthorized, match="WAYFARE_NROD_USER"):
        acquire.credentials("NROD")


def test_half_a_credential_pair_is_not_enough(monkeypatch):
    monkeypatch.setenv("WAYFARE_NROD_USER", "someone@example.com")
    monkeypatch.delenv("WAYFARE_NROD_PASS", raising=False)
    with pytest.raises(acquire.Unauthorized):
        acquire.credentials("NROD")


def test_a_source_with_credentials_sends_them(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_NROD_USER", "someone@example.com")
    monkeypatch.setenv("WAYFARE_NROD_PASS", "hunter2")
    seen = {}

    def fake_get(url, headers=None, auth=None, **k):
        seen["auth"] = auth
        return response(200, b"DATA")

    monkeypatch.setattr(acquire.requests, "get", fake_get)
    src = acquire.Source("cif", "http://x/c.gz", "c.gz", credentials_from="NROD")
    acquire._stream(src, tmp_path / "c.gz.part")
    assert seen["auth"] == ("someone@example.com", "hunter2")


def test_a_source_without_credentials_sends_none(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, headers=None, auth=None, **k):
        seen["auth"] = auth
        return response(200, b"DATA")

    monkeypatch.setattr(acquire.requests, "get", fake_get)
    src = acquire.Source("osm", "http://x/y.pbf", "y.pbf")
    acquire._stream(src, tmp_path / "y.pbf.part")
    assert seen["auth"] is None


def test_a_401_is_refused_rather_than_raised_as_a_transfer_fault(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_NROD_USER", "u")
    monkeypatch.setenv("WAYFARE_NROD_PASS", "p")
    monkeypatch.setattr(acquire.requests, "get", lambda *a, **k: response(401, b"nope"))
    src = acquire.Source("cif", "http://x/c.gz?type=x", "c.gz", credentials_from="NROD")
    with pytest.raises(acquire.Unauthorized, match="401"):
        acquire._stream(src, tmp_path / "c.gz.part")


def test_a_refusal_is_not_retried(tmp_path, monkeypatch):
    """The whole point of the separate class: five attempts change nothing."""
    monkeypatch.setenv("WAYFARE_NROD_USER", "u")
    monkeypatch.setenv("WAYFARE_NROD_PASS", "p")
    monkeypatch.setattr(config, "DOWNLOAD_BACKOFF", 0.0)
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return response(403, b"nope")

    monkeypatch.setattr(acquire.requests, "get", fake_get)
    src = acquire.Source("cif", "http://x/c.gz", "c.gz", credentials_from="NROD")
    with pytest.raises(acquire.Unauthorized):
        acquire.download(src, tmp_path)
    assert calls["n"] == 1


def test_an_unset_credential_stops_before_any_request(tmp_path, monkeypatch):
    monkeypatch.delenv("WAYFARE_NROD_USER", raising=False)
    monkeypatch.delenv("WAYFARE_NROD_PASS", raising=False)

    def boom(*a, **k):
        raise AssertionError("no request should be made without credentials")

    monkeypatch.setattr(acquire.requests, "get", boom)
    src = acquire.Source("cif", "http://x/c.gz", "c.gz", credentials_from="NROD")
    with pytest.raises(acquire.Unauthorized):
        acquire.download(src, tmp_path)
