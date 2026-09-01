"""Artifact integrity.

Three tiers, strongest first:

1. The release metadata carries a sha512 or sha256. Verify against it.
2. No server checksum, but we have installed this exact version before. Compare
   against the digest we recorded then; a mismatch is a hard failure.
3. No server checksum and a version we have never seen. Record the digest and
   proceed, having said so.

Tier 3 does not authenticate anything and is not pretended to. What the pin
store buys is that tier 3 happens once per version rather than every time.
Strict mode removes tier 3 entirely.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

PIN_STORE_VERSION = 1
_CHUNK = 1024 * 1024


class IntegrityError(Exception):
    """Raised when an artifact fails verification."""


def digest_file(path: str, algorithm: str = "sha256") -> str:
    """Return the hex digest of a file, read incrementally."""
    hasher = hashlib.new(algorithm)
    with open(path, "rb") as fdesc:
        while chunk := fdesc.read(_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def default_pin_store() -> str:
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(state_home, "agup", "pins.json")


class PinStore:
    """Persistent record of digests observed for versions lacking server checksums."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or default_pin_store()

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fdesc:
                data = json.load(fdesc)
        except (OSError, json.JSONDecodeError):
            return {"version": PIN_STORE_VERSION, "pins": {}}
        if not isinstance(data, dict) or data.get("version") != PIN_STORE_VERSION:
            return {"version": PIN_STORE_VERSION, "pins": {}}
        pins = data.get("pins")
        return {
            "version": PIN_STORE_VERSION,
            "pins": pins if isinstance(pins, dict) else {},
        }

    def _write(self, data: dict) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fdesc:
            json.dump(data, fdesc, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    @staticmethod
    def _key(component: str, version: str) -> str:
        return f"{component}:{version}"

    def get(self, component: str, version: str) -> Optional[str]:
        entry = self._read()["pins"].get(self._key(component, version))
        if isinstance(entry, dict):
            value = entry.get("sha256")
            return value.lower() if isinstance(value, str) else None
        return None

    def put(self, component: str, version: str, sha256: str) -> None:
        data = self._read()
        data["pins"][self._key(component, version)] = {"sha256": sha256.lower()}
        self._write(data)

    def forget(self, component: str, version: str) -> bool:
        data = self._read()
        removed = data["pins"].pop(self._key(component, version), None) is not None
        if removed:
            self._write(data)
        return removed

    def entries(self) -> dict[str, str]:
        return {
            key: entry["sha256"]
            for key, entry in self._read()["pins"].items()
            if isinstance(entry, dict) and isinstance(entry.get("sha256"), str)
        }


@dataclass
class VerificationResult:
    """How an artifact was verified, for reporting."""

    tier: str
    message: str


def verify_artifact(
    path: str,
    component: str,
    version: str,
    *,
    sha512: Optional[str] = None,
    sha256: Optional[str] = None,
    store: Optional[PinStore] = None,
    strict: bool = False,
) -> VerificationResult:
    """Verify a downloaded artifact, raising IntegrityError on any mismatch."""
    if sha512:
        actual = digest_file(path, "sha512")
        if actual.lower() != sha512.lower():
            raise IntegrityError(
                f"sha512 mismatch for {component} {version}: "
                f"server said {sha512[:16]}…, archive is {actual[:16]}…"
            )
        return VerificationResult("server-sha512", "Verified against server sha512.")

    if sha256:
        actual = digest_file(path, "sha256")
        if actual.lower() != sha256.lower():
            raise IntegrityError(
                f"sha256 mismatch for {component} {version}: "
                f"server said {sha256[:16]}…, archive is {actual[:16]}…"
            )
        return VerificationResult("server-sha256", "Verified against server sha256.")

    actual = digest_file(path, "sha256")
    store = store or PinStore()
    pinned = store.get(component, version)

    if pinned is not None:
        if pinned != actual:
            raise IntegrityError(
                f"Pin mismatch for {component} {version}. Recorded {pinned[:16]}…, "
                f"downloaded {actual[:16]}…. The artifact for a version already seen "
                f"has changed; refusing to install. If this is a legitimate respin, "
                f"clear it with: agup pins --forget {component}:{version}"
            )
        return VerificationResult(
            "pinned", f"No server checksum; matches digest pinned for {version}."
        )

    if strict:
        raise IntegrityError(
            f"No server checksum for {component} {version} and no recorded pin. "
            f"Strict mode refuses first-sight artifacts."
        )

    store.put(component, version, actual)
    return VerificationResult(
        "first-sight",
        f"No server checksum; recorded {actual[:16]}… as the pin for {version}. "
        f"This first download is unverified.",
    )
