# Three stages, so no compiler ever reaches the shipped image.
#
#   tippecanoe  builds the felt fork from source -- no distro packages it, and
#               only that fork writes PMTiles
#   deps        compiles the Python dependencies, pycairo among them, into a venv
#   runtime     copies both results in and carries no build tooling at all
#
# The alternative -- installing build-essential in the runtime stage and purging
# it afterwards -- leaves the compiler in that layer's history whatever the purge
# does, and rebuilds it on every source change. Splitting the stages means editing
# wayfare/ rebuilds only the last few layers.

FROM debian:bookworm-slim AS tippecanoe
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git libsqlite3-dev zlib1g-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# The six binaries ship with their debug symbols: 63 MB of them against 4 MB of
# code, and the image carries no debugger to read them with. strip in this stage
# rather than the runtime one, because a delete in a later layer hides a file
# without shrinking the image -- every layer keeps what it was built with.
RUN git clone --depth 1 --branch 2.79.0 https://github.com/felt/tippecanoe.git /src \
    && make -C /src -j"$(nproc)" \
    && make -C /src install \
    && strip /usr/local/bin/*


FROM python:3.12-slim-bookworm AS deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libcairo2-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

# A venv rather than the system site-packages, because it is one self-contained
# directory to copy forward -- and one without pip in it, which is 13 MB nothing
# in the image ever runs. Every install here goes through `pip --python`, the
# base image's own pip pointed at another interpreter, so the venv never needs
# one of its own.
RUN python -m venv --without-pip /opt/venv

WORKDIR /app
# Dependencies resolve from pyproject alone, so this layer survives every change
# to wayfare/ and only rebuilds when the dependency list moves. README stays out
# on purpose: pyproject carries no `readme` field, so copying it here would buy
# nothing and make every docs edit recompile pycairo.
COPY pyproject.toml ./
RUN mkdir -p wayfare && touch wayfare/__init__.py \
    && pip --python /opt/venv/bin/python install --no-cache-dir '.[art]' \
    && pip --python /opt/venv/bin/python uninstall -y wayfare

# The `art` extra is three wheels of which wayfare uses a thin slice, and pyarrow
# alone was 156 MB of the image. What comes out is what running the slice cannot
# reach: Arrow Flight is an RPC client and server nothing here starts, and the
# headers, the Cython declarations and both test suites exist to build against
# the wheel rather than to run it.
#
# What stays is not a judgement. `libarrow_python.so` -- which `import pyarrow`
# loads -- declares DT_NEEDED on compute, parquet, dataset, substrait and acero,
# so deleting any of those breaks the import however unused the feature looks.
# Read the NEEDED entries before removing another one.
#
# strip is pyarrow-only for a reason worth keeping: numpy.libs holds an
# auditwheel-patched OpenBLAS whose program headers strip rewrites into something
# the loader refuses -- "ELF load command address/offset not page-aligned",
# raised at `import numpy` and pointing nowhere near the build that caused it.
RUN P=/opt/venv/lib/python3.12/site-packages/pyarrow \
    && N=/opt/venv/lib/python3.12/site-packages/numpy \
    && rm -rf "$P"/tests "$P"/include "$P"/src "$P"/*.pxd "$P"/*.pxi \
    && rm -f "$P"/libarrow_flight.so* "$P"/libarrow_python_flight.so* \
             "$P"/_flight*.so "$P"/flight.py \
    && rm -rf "$N"/f2py "$N"/_core/include "$N"/_core/lib "$N"/_pyinstaller \
    && find "$N" -type d -name tests -prune -exec rm -rf {} + \
    && find "$P" -name '*.so*' -type f -exec strip --strip-unneeded {} +


FROM python:3.12-slim-bookworm

# Runtime shared libraries only: tippecanoe's, and cairo's for the art renderer.
#
# curl was here for "the compose healthcheck", and there is no healthcheck this
# image runs. The only one in the project is valhalla's, against port 8002, and it
# executes inside valhalla's own container -- both the committed Compose file and
# the two the Ansible role renders on the deployed host agree on that. Restoring
# it is this line plus 1.1 MB, and a healthcheck added for `web` later would need it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsqlite3-0 zlib1g libcairo2 \
    && rm -rf /var/lib/apt/lists/*


# The whole directory rather than a tippecanoe* glob. `make install` lays down
# six binaries and `tile-join` is the only one not carrying the prefix, so the
# glob dropped exactly one -- silently, since nothing in the build or the image
# looks for it. publish.py runs tippecanoe twice over different zoom ranges and
# uses tile-join to concatenate the passes into a single PMTiles file, so a
# national run spent its hours matching, exported the GeoJSONL, and only then
# died on a missing executable. This stage's base image ships an empty
# /usr/local/bin, so copying the directory carries tippecanoe's install output
# and nothing else.
COPY --from=tippecanoe /usr/local/bin/ /usr/local/bin/
COPY --from=deps /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH

# Built from /src and not from /app, because WORKDIR /app puts the working
# directory ahead of site-packages on sys.path: a copy of wayfare/ left under
# /app is the one every `import wayfare` resolves to, and the copy pip installed
# is then dead weight that never executes. The source is deleted in the same
# layer that installs it, so exactly one copy is importable and it is pip's.
#
# `pip` here is the base image's, since the venv has none; --no-deps because the
# venv already holds every dependency. This layer is the package and the
# `wayfare` console script ENTRYPOINT runs -- seconds, not minutes.
#
# Importing that pip writes its own bytecode beside it, 5 MB of cache for a
# process that runs once at build time and never again. Refusing the write costs
# nothing: pip compiles what it *installs* through compileall, which the variable
# does not reach, so wayfare still arrives with its .pyc files.
WORKDIR /src
COPY pyproject.toml ./
COPY wayfare ./wayfare
RUN PYTHONDONTWRITEBYTECODE=1 \
    pip --python "$VIRTUAL_ENV/bin/python" install --no-cache-dir --no-deps . \
    && rm -rf /src

# The viewer and the range-capable static server, so the `web` service serves
# straight out of this image.
WORKDIR /app
COPY web ./web
COPY scripts ./scripts

# Not root, but the data volume has to be writable by whoever we become.
RUN useradd --uid 10001 --create-home wayfare && mkdir -p /data && chown wayfare /data
USER wayfare

ENV WAYFARE_DATA=/data
VOLUME /data

ENTRYPOINT ["wayfare"]
CMD ["status"]
