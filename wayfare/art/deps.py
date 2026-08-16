"""The two lazy imports of the `art` extra.

`pycairo`, `numpy` and `pyarrow` are an optional install, and nothing outside this
package imports any of the three. Here rather than at the top of the module that
uses each, so that importing `wayfare.art` for its presets, its projection or its
query costs none of them -- and so the message a missing one produces is written
once.
"""

from __future__ import annotations

from typing import Any


def _require_cairo() -> Any:
    """Imported lazily so `import wayfare.art` works without the extra installed.

    The presets, the projection and the query are all useful to a caller that only
    wants coordinates, and pycairo pulls in a system libcairo that a headless
    pipeline box has no reason to carry.
    """
    try:
        import cairo
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "rendering needs pycairo. Install the extra with: pip install -e '.[art]'"
        ) from exc
    return cairo


def _require_numpy() -> Any:
    """Also lazy, and for the same reason as :func:`_require_cairo`.

    Only :meth:`Projection.batch` needs it, so a caller reading coordinates out of
    the database never pays for the import.
    """
    try:
        import numpy
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "rendering needs numpy. Install the extra with: pip install -e '.[art]'"
        ) from exc
    return numpy
