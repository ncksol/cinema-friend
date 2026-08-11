"""Migration SQL must ship with the built distribution, not just the source tree."""

from __future__ import annotations

import tomllib
from fnmatch import fnmatch
from importlib.resources import files
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _PROJECT_ROOT / "src" / "cinema_friend" / "storage" / "migrations"


def _declared_globs() -> list[str]:
    pyproject = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text())
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    return list(package_data["cinema_friend.storage"])


def test_every_migration_file_is_declared_as_package_data() -> None:
    """Without this declaration a built wheel omits the migrations and migrate() fails."""
    globs = _declared_globs()
    migrations = sorted(path.name for path in _MIGRATIONS_DIR.glob("*.sql"))
    assert migrations, "no migration files found to verify"
    for name in migrations:
        assert any(fnmatch(f"migrations/{name}", pattern) for pattern in globs), (
            f"migrations/{name} is not covered by package-data globs {globs}"
        )


def test_migrations_are_reachable_as_package_resources() -> None:
    resource = files("cinema_friend.storage").joinpath("migrations", "001_initial.sql")
    assert resource.is_file()
    assert "CREATE TABLE watches" in resource.read_text()
