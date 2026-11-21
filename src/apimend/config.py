"""Settings from your project's pyproject.toml.

    [tool.apimend]
    request-functions = ["send", "api.call"]

`request-functions` names your own functions that send HTTP requests, for wrappers
apimend cannot prove on its own (for example because they are defined in another file).
apimend reads the nearest pyproject.toml in the folder it scans or a folder above it.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib


class ConfigError(Exception):
    """Raised when the [tool.apimend] settings cannot be used."""


def find_pyproject(start: str | Path) -> Path | None:
    """The nearest pyproject.toml in `start` or a folder above it."""
    folder = Path(start).resolve()
    for candidate in (folder, *folder.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate / "pyproject.toml"
    return None


def request_functions(start: str | Path) -> list[str]:
    """The request functions listed in the nearest pyproject.toml, if any."""
    pyproject = find_pyproject(start)
    if pyproject is None:
        return []
    try:
        settings = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {pyproject}: {exc}") from exc
    tool = settings.get("tool", {})
    names = tool.get("apimend", {}).get("request-functions", []) if isinstance(tool, dict) else []
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise ConfigError(
            f"{pyproject}: [tool.apimend] request-functions must be a list of function names"
        )
    return names
