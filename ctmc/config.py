"""Load config.yaml into a plain nested dict with attribute access."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class Cfg(dict):
    """dict that also allows cfg.section.key access."""

    def __getattr__(self, name: str) -> Any:
        try:
            value = self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return Cfg(value) if isinstance(value, dict) else value


def load(path: str | Path = "config.yaml") -> Cfg:
    with open(path, encoding="utf-8") as fh:
        return Cfg(yaml.safe_load(fh))
