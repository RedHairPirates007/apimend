"""Settings from your project's pyproject.toml.

    [tool.apimend]
    request-functions = ["send", "api.call"]
    renames = { quantity = "customer_id" }

- `request-functions` names your own functions that send HTTP requests, for wrappers
  apimend cannot prove on its own (for example because they are defined in another file).
- `renames` approves renames apimend could not confirm by itself (old name = new name).

apimend reads the nearest pyproject.toml in the folder it scans or a folder above it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib


class ConfigError(Exception):
    """Raised when the [tool.apimend] settings cannot be used."""


@dataclass
class Settings:
    request_functions: list[str] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)  # approved (old, new)


def find_pyproject(start: str | Path) -> Path | None:
    """The nearest pyproject.toml in `start` or a folder above it."""
    folder = Path(start).resolve()
    for candidate in (folder, *folder.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate / "pyproject.toml"
    return None


def load(start: str | Path) -> Settings:
    """The [tool.apimend] settings in the nearest pyproject.toml, if any."""
    pyproject = find_pyproject(start)
    if pyproject is None:
        return Settings()
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {pyproject}: {exc}") from exc
    tool = document.get("tool", {})
    section = tool.get("apimend", {}) if isinstance(tool, dict) else {}
    if not isinstance(section, dict):
        raise ConfigError(f"{pyproject}: [tool.apimend] must be a table")

    names = section.get("request-functions", [])
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise ConfigError(
            f"{pyproject}: [tool.apimend] request-functions must be a list of function names"
        )
    renames = section.get("renames", {})
    if not isinstance(renames, dict) or not all(
        isinstance(new, str) and new and old for old, new in renames.items()
    ):
        raise ConfigError(
            f'{pyproject}: [tool.apimend] renames must map old names to new names, e.g. {{ size = "pizza_size" }}'
        )
    return Settings(names, list(renames.items()))


def request_functions(start: str | Path) -> list[str]:
    """The request functions listed in the nearest pyproject.toml, if any."""
    return load(start).request_functions


def parse_rename(text: str) -> tuple[str, str]:
    """Read an approved rename written as OLD=NEW."""
    old, sep, new = text.partition("=")
    old, new = old.strip(), new.strip()
    if not sep or not old or not new or "=" in new:
        raise ConfigError(f"--rename {text}: write it as OLD=NEW, e.g. --rename size=pizza_size")
    return old, new
