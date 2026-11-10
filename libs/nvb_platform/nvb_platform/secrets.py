"""Secrets are always read from files (mounted Kubernetes Secrets); values are never logged."""

from __future__ import annotations

from pathlib import Path


def read_secret(path: str | None) -> str | None:
    if not path:
        return None
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None
