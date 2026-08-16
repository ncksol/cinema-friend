#!/usr/bin/env python3
"""Check that an installed cinema-friend can read the files it needs at runtime.

``./install.sh`` runs ``pip install --upgrade .``, so what reaches a user's machine is a
built distribution and not the checkout beside it. The migrations are the part of the
package that a build can silently drop: they are data files, nothing imports them, and
:meth:`Database.migrate` is the first thing to notice, at which point the failure is a
fresh install with no schema rather than a build error.

The suite cannot catch that. A development checkout is installed editable, so
``importlib.resources`` resolves to ``src/`` and finds every migration whether or not the
build would have carried it. This script is the same assertion made against a real
install: run it with the interpreter of an environment the distribution was installed
into, and it refuses to pass if that environment is resolving the package from this
checkout instead.

Exit codes: ``0`` if the installed package carries every migration, ``1`` otherwise.
"""

from __future__ import annotations

import sys
from importlib.resources import files
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_MIGRATIONS = _REPO_ROOT / "src" / "cinema_friend" / "storage" / "migrations"


class VerificationError(Exception):
    """The installed distribution is not one this project would ship."""


def _installed_package_root() -> Path:
    import cinema_friend

    locations = list(getattr(cinema_friend, "__path__", []))
    if len(locations) != 1:
        raise VerificationError(f"cinema_friend resolves to {locations or 'nothing'}")
    return Path(locations[0]).resolve()


def verify() -> None:
    """Raise :class:`VerificationError` unless the installed package is complete."""
    package_root = _installed_package_root()
    source_root = (_REPO_ROOT / "src" / "cinema_friend").resolve()
    if package_root == source_root or source_root in package_root.parents:
        raise VerificationError(
            f"cinema_friend is being imported from the checkout ({package_root}); "
            "run this with the interpreter of an environment the distribution was "
            "installed into"
        )

    expected = sorted(path.name for path in _SOURCE_MIGRATIONS.glob("*.sql"))
    if not expected:
        raise VerificationError(f"no migrations found in {_SOURCE_MIGRATIONS}")

    migrations = files("cinema_friend.storage").joinpath("migrations")
    for name in expected:
        resource = migrations.joinpath(name)
        if not resource.is_file():
            raise VerificationError(f"the installed distribution is missing migrations/{name}")
        if not resource.read_text().strip():
            raise VerificationError(f"the installed migrations/{name} is empty")

    print(f"installed at {package_root} with {len(expected)} migration(s): {', '.join(expected)}")


def main() -> int:
    try:
        verify()
    except VerificationError as error:
        print(f"distribution error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a process, not a test
    raise SystemExit(main())
