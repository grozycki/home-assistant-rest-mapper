"""Both languages hold the same keys, and flows use keys that exist."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent / "custom_components" / "rest_mapper"


def keys(value: object, prefix: str = "") -> set[str]:
    """List key paths."""
    if not isinstance(value, dict):
        return {prefix}
    return set().union(*(keys(item, f"{prefix}/{key}") for key, item in value.items()))


def load(language: str) -> dict[str, dict[str, object]]:
    """Read one language."""
    data: dict[str, dict[str, object]] = json.loads(
        (ROOT / "translations" / f"{language}.json").read_text(encoding="utf-8")
    )
    return data


def test_languages_match() -> None:
    assert keys(load("en")) == keys(load("pl"))


def test_error_keys_exist() -> None:
    code = "".join(path.read_text(encoding="utf-8") for path in ROOT.glob("*.py"))
    english = load("en")
    config = english["config"]
    errors = config["error"]
    assert isinstance(errors, dict)
    for key in errors:
        assert f'"{key}"' in code
    for key in english["exceptions"]:
        assert f'"{key}"' in code
    for key in english["issues"]:
        assert f'"{key}"' in code
