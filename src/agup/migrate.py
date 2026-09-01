"""User profile migration.

A major release can change ``dataFolderName`` in product.json. When it does,
the new build starts against an empty profile and looks, from the user's side,
like every setting, extension and chat history has been lost -- while the old
data sits untouched in the old directory.

This module detects that case and offers to carry the data across. It never
migrates without being asked, and it never deletes the source.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from typing import Optional

#: Subdirectories of the config profile worth carrying across. Caches and
#: Chromium scratch directories are deliberately excluded -- they are large,
#: version-specific, and regenerated on launch.
_CONFIG_PAYLOAD = ("User", "argv.json", "machineid")

_SKIP_CONFIG = frozenset(
    {
        "Cache",
        "CachedData",
        "CachedProfilesData",
        "CachedConfigurations",
        "CachedExtensionVSIXs",
        "Code Cache",
        "GPUCache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
        "Crashpad",
        "logs",
        "blob_storage",
        "Service Worker",
        "Session Storage",
        "DevToolsActivePort",
    }
)


@dataclass
class Profile:
    """Where one version keeps its data."""

    data_folder: str
    config_dir: str

    @property
    def exists(self) -> bool:
        return os.path.isdir(self.data_folder) or os.path.isdir(self.config_dir)

    def size_bytes(self) -> int:
        total = 0
        for root in (self.data_folder, self.config_dir):
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        continue
        return total


def read_data_folder_name(install_dir: str) -> Optional[str]:
    """Read dataFolderName from a bundle's product.json."""
    for relative in (os.path.join("resources", "app", "product.json"), "product.json"):
        try:
            with open(os.path.join(install_dir, relative), "r", encoding="utf-8") as fdesc:
                data = json.load(fdesc)
        except (OSError, json.JSONDecodeError):
            continue
        value = data.get("dataFolderName")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def read_app_name(install_dir: str) -> Optional[str]:
    """Read nameLong, which is the config directory name under ~/.config."""
    for relative in (os.path.join("resources", "app", "product.json"), "product.json"):
        try:
            with open(os.path.join(install_dir, relative), "r", encoding="utf-8") as fdesc:
                data = json.load(fdesc)
        except (OSError, json.JSONDecodeError):
            continue
        value = data.get("nameLong")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def profile_for(data_folder_name: str, app_name: str) -> Profile:
    home = os.path.expanduser("~")
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return Profile(
        data_folder=os.path.join(home, data_folder_name),
        config_dir=os.path.join(config_home, app_name),
    )


def find_predecessor(
    new_data_folder: str, new_app_name: str, *, known: tuple[str, ...] = ()
) -> Optional[Profile]:
    """Look for an older profile whose data the new version cannot see.

    Only considers directories that exist and differ from the new profile, so
    a same-folder upgrade -- the common case -- returns None and nothing is
    offered.
    """
    home = os.path.expanduser("~")
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    new_profile = profile_for(new_data_folder, new_app_name)

    candidates: list[Profile] = []
    for folder in known:
        candidates.append(profile_for(folder, folder.lstrip(".").title()))

    # A build that renamed ".antigravity" to ".antigravity-ide" leaves the
    # original as a prefix of the new name; check that relation both ways.
    stem = new_data_folder.split("-")[0]
    if stem != new_data_folder:
        candidates.append(profile_for(stem, new_app_name.split()[0]))

    try:
        for entry in os.listdir(config_home):
            if entry != new_app_name and entry.split()[0] == new_app_name.split()[0]:
                candidates.append(
                    Profile(
                        data_folder=os.path.join(home, stem),
                        config_dir=os.path.join(config_home, entry),
                    )
                )
    except OSError:
        pass

    for candidate in candidates:
        if candidate.config_dir == new_profile.config_dir:
            continue
        if candidate.exists:
            return candidate
    return None


def _copy_tree(source: str, destination: str, *, skip: frozenset = frozenset()) -> int:
    """Copy a directory, forcing writable permissions on what we create.

    Preserving the source's read-only directory modes is what makes a naive
    recursive copy fail partway through: it creates a directory it then has no
    permission to write into.
    """
    copied = 0
    if not os.path.isdir(source):
        return 0
    for dirpath, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d not in skip]
        relative = os.path.relpath(dirpath, source)
        target_dir = destination if relative == "." else os.path.join(destination, relative)
        try:
            os.makedirs(target_dir, exist_ok=True)
            os.chmod(target_dir, 0o700)
        except OSError:
            continue
        for name in files:
            src = os.path.join(dirpath, name)
            dst = os.path.join(target_dir, name)
            try:
                shutil.copy2(src, dst)
                os.chmod(dst, 0o600)
                copied += 1
            except OSError:
                continue
    return copied


def migrate(
    source: Profile,
    destination: Profile,
    *,
    include_extensions: bool = False,
    backup: bool = True,
) -> dict[str, int]:
    """Copy a previous profile's data into a new one.

    Settings, chat history and workspace state live under the config
    directory; extensions live under the data folder and are re-downloadable,
    so they are opt-in.
    """
    result = {"config_files": 0, "data_files": 0}

    if backup and os.path.isdir(destination.config_dir):
        backup_path = f"{destination.config_dir}.agup-backup"
        if not os.path.exists(backup_path):
            try:
                shutil.copytree(destination.config_dir, backup_path, dirs_exist_ok=False)
            except (OSError, shutil.Error):
                pass

    result["config_files"] = _copy_tree(
        source.config_dir, destination.config_dir, skip=_SKIP_CONFIG
    )

    if include_extensions:
        result["data_files"] = _copy_tree(source.data_folder, destination.data_folder)

    return result


def describe(profile: Profile) -> str:
    """One-line summary of a profile for reporting."""
    size = profile.size_bytes()
    return f"{profile.config_dir} ({size / 1048576:.0f} MB)"
