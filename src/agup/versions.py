"""Version handling and release selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

_NUMERIC = re.compile(r"\d+")

UNKNOWN_VERSION = "0.0.0"


def parse_version(value: str) -> tuple[int, ...]:
    """Parse a version string into a comparable tuple.

    Tolerant by design: release feeds have carried things like '2.5.5',
    '1.23.2-1776332190' and '2.11.0+linux'. Everything non-numeric is a
    separator, and a missing component sorts as zero.
    """
    if not value:
        return (0,)
    parts = tuple(int(match.group()) for match in _NUMERIC.finditer(value))
    return parts or (0,)


def compare_versions(left: str, right: str) -> int:
    """Return -1, 0 or 1 comparing two version strings."""
    lhs, rhs = parse_version(left), parse_version(right)
    width = max(len(lhs), len(rhs))
    lhs += (0,) * (width - len(lhs))
    rhs += (0,) * (width - len(rhs))
    return (lhs > rhs) - (lhs < rhs)


def is_newer(candidate: str, installed: str) -> bool:
    """Whether candidate should replace installed."""
    if installed == UNKNOWN_VERSION:
        return True
    return compare_versions(candidate, installed) > 0


@dataclass
class Release:
    """One available release."""

    version: str
    download_url: Optional[str] = None
    execution_id: Optional[str] = None
    sha512: Optional[str] = None
    sha256: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Release":
        version = payload.get("version") or payload.get("name")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("Release payload has no usable version field")

        # The IDE and Hub feeds return version + execution_id only; the
        # artifact URL is constructed from those. The CLI manifest carries a
        # URL directly. Both shapes are valid.
        url = (
            payload.get("url")
            or payload.get("downloadUrl")
            or payload.get("download_url")
        )
        url = url.strip() if isinstance(url, str) and url.strip() else None

        exec_id = payload.get("executionId") or payload.get("execution_id")
        if url is None and not exec_id:
            raise ValueError(f"Release {version} has neither a URL nor an execution id")

        def _hex(field: str, width: int) -> Optional[str]:
            raw = payload.get(field)
            if raw is None:
                return None
            if not isinstance(raw, str):
                raise ValueError(f"{field} must be a string")
            cleaned = raw.strip().lower()
            if not re.fullmatch(rf"[0-9a-f]{{{width}}}", cleaned):
                raise ValueError(f"{field} is not a {width}-character hex digest")
            return cleaned

        return cls(
            version=version.strip(),
            download_url=url,
            execution_id=str(exec_id) if exec_id else None,
            sha512=_hex("sha512", 128),
            sha256=_hex("sha256", 64),
        )


def select_latest(releases: list[Release]) -> Optional[Release]:
    """Pick the highest-versioned release."""
    if not releases:
        return None
    return max(releases, key=lambda rel: parse_version(rel.version))
