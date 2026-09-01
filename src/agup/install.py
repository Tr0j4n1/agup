"""Local installation state and filesystem operations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - used only for fixed argv process lookups
import tarfile
import tempfile
from dataclasses import dataclass
from typing import Optional

from .versions import UNKNOWN_VERSION


class InstallError(Exception):
    """Raised when a local installation operation cannot complete."""


@dataclass
class Paths:
    """Where each component lives for a given scope."""

    ide_dir: str
    hub_dir: str
    cli_bin: str
    bin_dir: str

    def with_overrides(self, **overrides: Optional[str]) -> "Paths":
        """Return a copy with explicit path overrides applied."""
        return Paths(
            ide_dir=overrides.get("ide_dir") or self.ide_dir,
            hub_dir=overrides.get("hub_dir") or self.hub_dir,
            cli_bin=overrides.get("cli_bin") or self.cli_bin,
            bin_dir=overrides.get("bin_dir") or self.bin_dir,
        )

    @classmethod
    def for_scope(cls, scope: str) -> "Paths":
        if scope == "system":
            return cls(
                ide_dir="/opt/Antigravity-IDE",
                hub_dir="/opt/Antigravity",
                cli_bin="/usr/local/bin/agy",
                bin_dir="/usr/local/bin",
            )
        if scope == "user":
            home = os.path.expanduser("~")
            return cls(
                ide_dir=os.path.join(home, "opt", "Antigravity-IDE"),
                hub_dir=os.path.join(home, "opt", "Antigravity"),
                cli_bin=os.path.join(home, ".local", "bin", "agy"),
                bin_dir=os.path.join(home, ".local", "bin"),
            )
        raise InstallError(f"Unknown scope {scope!r}; expected 'user' or 'system'")

    def for_component(self, component: str) -> str:
        return {"ide": self.ide_dir, "hub": self.hub_dir, "cli": self.cli_bin}[component]


#: Where a version might live, in decreasing order of trust.
#:
#: Antigravity is a VS Code fork, so ``package.json`` and ``product.json``
#: both carry a ``version`` field holding the *upstream Code* version --
#: 1.107.0 on a build whose actual Antigravity version is 1.23.2. Only
#: ``ideVersion`` carries the number the release feed speaks in, so it has to
#: be consulted first or every comparison is against the wrong lineage.
_VERSION_SOURCES: tuple[tuple[str, str], ...] = (
    (os.path.join("resources", "app", "product.json"), "ideVersion"),
    (os.path.join("resources", "app", "product.json"), "version"),
    (os.path.join("resources", "app", "package.json"), "version"),
    ("product.json", "ideVersion"),
    ("package.json", "version"),
)


def read_installed_version(target: str) -> str:
    """Determine the installed version of a component.

    Returns UNKNOWN_VERSION rather than guessing when nothing is found, which
    the caller treats as 'not installed'.
    """
    if not os.path.exists(target):
        return UNKNOWN_VERSION

    for relative, key in _VERSION_SOURCES:
        candidate = os.path.join(target, relative)
        try:
            with open(candidate, "r", encoding="utf-8") as fdesc:
                data = json.load(fdesc)
        except (OSError, json.JSONDecodeError):
            continue
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return UNKNOWN_VERSION


def read_product_identity(target: str) -> dict[str, str]:
    """Read identifying fields from a bundle's product.json.

    Used to check that a directory holds the component we think it does before
    replacing it -- writing an IDE tarball over a Hub install would otherwise
    be silent.
    """
    for relative in (os.path.join("resources", "app", "product.json"), "product.json"):
        try:
            with open(os.path.join(target, relative), "r", encoding="utf-8") as fdesc:
                data = json.load(fdesc)
        except (OSError, json.JSONDecodeError):
            continue
        return {
            key: str(data[key])
            for key in ("applicationName", "nameLong", "dataFolderName", "ideVersion")
            if isinstance(data.get(key), str)
        }
    return {}


def is_dpkg_owned(target: str) -> bool:
    """Whether a path is claimed by an installed Debian package.

    Overwriting dpkg-managed files leaves the package database describing a
    tree that no longer exists, and the next apt upgrade will fight whatever
    replaced it.
    """
    if not shutil.which("dpkg-query"):
        return False
    probe = os.path.join(target, "resources", "app", "product.json")
    for path in (probe, target):
        try:
            proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
                ["dpkg-query", "-S", path],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if proc.returncode == 0 and proc.stdout.strip():
            return True
    return False


def read_binary_version(binary: str, flag: str = "--version") -> str:
    """Ask a binary for its version."""
    if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        return UNKNOWN_VERSION
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            [binary, flag],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_VERSION
    output = (proc.stdout or proc.stderr or "").strip().splitlines()
    return output[0].strip() if output else UNKNOWN_VERSION


def _ancestry(pid: Optional[int] = None) -> set[int]:
    """Return our own PID and every ancestor up to init.

    Needed because a full-command-line process match will happily match the
    shell that invoked us, if the user happened to type the application name on
    that command line. Without this, running the updater from a terminal where
    you just mentioned the app makes it refuse to update.
    """
    current = os.getpid() if pid is None else pid
    seen: set[int] = set()
    while current and current > 1 and current not in seen:
        seen.add(current)
        try:
            with open(f"/proc/{current}/stat", "r", encoding="utf-8") as fdesc:
                fields = fdesc.read().rsplit(")", 1)[-1].split()
            current = int(fields[1])
        except (OSError, IndexError, ValueError):
            break
    return seen


def running_pids(*patterns: str) -> list[str]:
    """Return PIDs of processes matching any pattern, excluding our own tree."""
    excluded = _ancestry()
    found: set[int] = set()

    for pattern in patterns:
        try:
            proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue

        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.isdigit():
                continue
            pid = int(line)
            if pid in excluded:
                continue
            # Confirm the match is the executable itself rather than an
            # incidental mention somewhere in another process's arguments.
            if _executable_matches(pid, pattern):
                found.add(pid)

    return [str(pid) for pid in sorted(found)]


def _executable_matches(pid: int, pattern: str) -> bool:
    """Whether the process's own executable corresponds to the pattern."""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        # No permission or already exited; fall back to argv[0] only.
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fdesc:
                argv0 = fdesc.read().split(b"\0", 1)[0].decode("utf-8", "replace")
        except OSError:
            return False
        return pattern.lower() in argv0.lower()
    return pattern.lower() in exe.lower()


def is_writable(path: str) -> bool:
    """Whether we could create or replace path."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return os.access(probe, os.W_OK) if probe else False


def extract_tarball(archive: str, target_dir: str) -> None:
    """Extract a gzip tarball using data-filter semantics.

    The data filter blocks absolute paths, parent traversal, symlinks pointing
    outside the destination, and device nodes. Without it a hostile archive can
    write anywhere the process can.
    """
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(path=target_dir, filter="data")  # nosec B202 - filtered
    except (tarfile.TarError, OSError) as err:
        raise InstallError(f"Cannot extract {archive}: {err}") from err


def find_payload_root(extract_dir: str, hints: tuple[str, ...]) -> str:
    """Locate the real payload directory inside an extracted tree."""
    for hint in hints:
        candidate = os.path.join(extract_dir, hint)
        if os.path.isdir(candidate):
            return candidate

    entries = [
        os.path.join(extract_dir, name)
        for name in os.listdir(extract_dir)
        if os.path.isdir(os.path.join(extract_dir, name))
    ]
    if len(entries) == 1:
        return entries[0]

    raise InstallError(
        f"Cannot identify payload root in {extract_dir}; "
        f"looked for {hints} and found {len(entries)} directories"
    )


def swap_directory(payload: str, target: str) -> None:
    """Replace target with payload, keeping a rollback copy until it succeeds."""
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    backup: Optional[str] = None
    if os.path.exists(target):
        backup = tempfile.mkdtemp(prefix=".agup-rollback-", dir=parent or None)
        backup = os.path.join(backup, os.path.basename(target))
        shutil.move(target, backup)

    try:
        shutil.move(payload, target)
    except OSError as err:
        if backup and os.path.exists(backup):
            shutil.move(backup, target)
        raise InstallError(f"Cannot install into {target}: {err}") from err

    if backup:
        shutil.rmtree(os.path.dirname(backup), ignore_errors=True)


def link_command(source: str, link_path: str) -> None:
    """Create or refresh a symlink."""
    parent = os.path.dirname(link_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.islink(link_path) or os.path.exists(link_path):
        try:
            os.remove(link_path)
        except OSError as err:
            raise InstallError(f"Cannot replace {link_path}: {err}") from err
    try:
        os.symlink(source, link_path)
    except OSError as err:
        raise InstallError(f"Cannot link {link_path}: {err}") from err
