"""Desktop entry and icon installation.

Without this a bundle installed under ~/opt is invisible to the application
menu -- the symlink works from a shell and nowhere else. Entries are written
to the XDG user data directory so no root is needed for user-scope installs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv, no shell
from dataclasses import dataclass
from typing import Optional

#: Where a bundle keeps its icon, relative to the install root. VS Code forks
#: vary in layout between releases, so several are tried.
_ICON_CANDIDATES = (
    os.path.join("resources", "app", "resources", "linux", "code.png"),
    os.path.join("resources", "app", "resources", "linux", "icon.png"),
    os.path.join("resources", "app", "media", "icon.png"),
    "icon.png",
)

_CATEGORIES = "Development;IDE;TextEditor;"


def user_data_dir() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )


@dataclass
class DesktopTarget:
    """Where a component's menu integration goes."""

    applications_dir: str
    theme_base: str
    private_dir: str
    menu_caches: tuple[str, ...] = ()

    @classmethod
    def for_scope(cls, scope: str) -> "DesktopTarget":
        if scope == "system":
            return cls(
                applications_dir="/usr/local/share/applications",
                theme_base="/usr/local/share",
                private_dir="/usr/local/share/agup-icons",
            )
        base = user_data_dir()
        cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache"
        )
        return cls(
            applications_dir=os.path.join(base, "applications"),
            theme_base=base,
            private_dir=os.path.join(base, "agup-icons"),
            menu_caches=(
                os.path.join(cache, "xfce4", "menu"),
                os.path.join(cache, "menus"),
            ),
        )


def find_icon(install_dir: str) -> Optional[str]:
    """Locate the bundle's icon, falling back to a shallow search."""
    for relative in _ICON_CANDIDATES:
        candidate = os.path.join(install_dir, relative)
        if os.path.isfile(candidate):
            return candidate

    for root, dirs, files in os.walk(install_dir):
        # Keep the walk shallow; a full Electron tree is enormous.
        if root[len(install_dir) :].count(os.sep) >= 4:
            dirs[:] = []
            continue
        for name in files:
            if name in ("code.png", "icon.png", "antigravity.png"):
                return os.path.join(root, name)

    # Plain Electron apps ship no loose icon; it lives inside app.asar.
    asar = os.path.join(install_dir, "resources", "app.asar")
    if os.path.isfile(asar):
        cache = os.path.join(install_dir, ".agup-icon.png")
        if os.path.isfile(cache):
            return cache
        return extract_asar_icon(asar, cache)
    return None


def read_png_size(path: str) -> Optional[tuple[int, int]]:
    """Return (width, height) of a PNG, or None if it is not readable as one.

    The icon theme spec matches on the directory name, and a lookup silently
    fails when a file's real dimensions disagree with the folder it sits in --
    a 1024x1024 icon dropped into 512x512/apps simply never renders.
    """
    try:
        with open(path, "rb") as fdesc:
            header = fdesc.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


#: Icon theme directories we are willing to install into.
_THEME_SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512, 1024)


def theme_dir_for(icon_path: str, base: str) -> Optional[str]:
    """Pick the hicolor directory matching an icon's real dimensions."""
    size = read_png_size(icon_path)
    if size is None or size[0] != size[1] or size[0] not in _THEME_SIZES:
        return None
    return os.path.join(base, "icons", "hicolor", f"{size[0]}x{size[0]}", "apps")


def install_icon(
    install_dir: str, icon_name: str, target: DesktopTarget
) -> Optional[str]:
    """Install the bundle icon and return the value to use in ``Icon=``.

    Prefers the icon theme when the file's dimensions map to a standard size.
    Falls back to an absolute path, which the spec allows and which works
    regardless of dimensions -- better a correct absolute path than a themed
    icon that never resolves.
    """
    source = find_icon(install_dir)
    if not source:
        return None

    themed = theme_dir_for(source, target.theme_base)
    if themed:
        try:
            os.makedirs(themed, exist_ok=True)
            shutil.copy2(source, os.path.join(themed, f"{icon_name}.png"))
            refresh_icon_cache(os.path.join(target.theme_base, "icons", "hicolor"))
            return icon_name
        except OSError:
            pass

    # Non-standard size, or the theme copy failed: use an absolute path.
    try:
        os.makedirs(target.private_dir, exist_ok=True)
        destination = os.path.join(target.private_dir, f"{icon_name}.png")
        shutil.copy2(source, destination)
        return destination
    except OSError:
        return None


def refresh_icon_cache(theme_root: str) -> None:
    """Rebuild the icon cache so a new icon is picked up without a re-login."""
    if not shutil.which("gtk-update-icon-cache"):
        return
    try:
        subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["gtk-update-icon-cache", "-f", "-t", theme_root],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def render_entry(
    *,
    name: str,
    executable: str,
    icon: str,
    comment: str,
    wm_class: str,
    categories: str = _CATEGORIES,
) -> str:
    """Build a .desktop file body."""
    return "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            f"Name={name}",
            f"Comment={comment}",
            f"Exec={executable} %F",
            f"Icon={icon}",
            "Terminal=false",
            f"Categories={categories}",
            "StartupNotify=true",
            f"StartupWMClass={wm_class}",
            "MimeType=text/plain;inode/directory;",
            "Actions=new-window;",
            "",
            "[Desktop Action new-window]",
            "Name=New Empty Window",
            f"Exec={executable} --new-window %F",
            "",
        )
    )


def refresh_menu(target: "DesktopTarget") -> None:
    """Reindex the desktop database and drop stale menu caches.

    XFCE's Whisker menu keeps its own cache that update-desktop-database does
    not touch, so a new entry can stay invisible until the cache is cleared or
    the panel restarted.
    """
    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(  # nosec B603 B607 - fixed argv, no shell
                ["update-desktop-database", target.applications_dir],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    for cache in target.menu_caches:
        shutil.rmtree(cache, ignore_errors=True)


def install_desktop_entry(
    component: str,
    install_dir: str,
    executable: str,
    *,
    scope: str = "user",
    version: str = "",
) -> tuple[Optional[str], bool]:
    """Write a .desktop entry for a component.

    Returns (entry_path, icon_installed). The second value matters: writing
    ``Icon=antigravity`` when no such icon exists produces an entry that looks
    fine on disk and renders blank, with nothing said about it.
    """
    spec = {
        "ide": ("Antigravity IDE", "antigravity-ide", "Antigravity", "AI-native code editor"),
        "hub": ("Antigravity", "antigravity", "Antigravity", "Antigravity"),
    }.get(component)
    if spec is None:
        return None, False

    name, slug, wm_class, comment = spec
    target = DesktopTarget.for_scope(scope)

    installed_icon = install_icon(install_dir, slug, target)
    icon = installed_icon or slug
    body = render_entry(
        name=name,
        executable=executable,
        icon=icon,
        comment=comment,
        wm_class=wm_class,
    )

    path = os.path.join(target.applications_dir, f"{slug}.desktop")
    try:
        os.makedirs(target.applications_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fdesc:
            fdesc.write(body)
        os.chmod(path, 0o644)
    except OSError:
        return None, bool(installed_icon)

    refresh_menu(target)
    return path, bool(installed_icon)


def remove_desktop_entry(component: str, *, scope: str = "user") -> bool:
    """Remove a previously installed entry."""
    slug = {"ide": "antigravity-ide", "hub": "antigravity"}.get(component)
    if slug is None:
        return False
    target = DesktopTarget.for_scope(scope)
    candidates = [
        os.path.join(target.applications_dir, f"{slug}.desktop"),
        os.path.join(target.private_dir, f"{slug}.png"),
    ]
    candidates += [
        os.path.join(target.theme_base, "icons", "hicolor", f"{n}x{n}", "apps", f"{slug}.png")
        for n in _THEME_SIZES
    ]
    removed = False
    for path in candidates:
        try:
            os.remove(path)
            removed = True
        except OSError:
            continue
    if removed:
        refresh_menu(target)
    return removed


# --- asar archive reading ---------------------------------------------------
#
# Electron apps that are not VS Code forks keep no loose icon on disk; the
# Antigravity Hub ships nothing but app.asar under resources/. asar is a plain
# container -- a length-prefixed JSON directory followed by concatenated file
# bodies -- so the icon can be pulled out without Node or any dependency.

_ASAR_ICON_NAMES = ("icon.png", "logo.png", "app.png", "tray.png", "icon@2x.png")


def _asar_header(path: str) -> Optional[tuple[dict, int]]:
    """Return (directory, data_offset) for an asar archive.

    The header is a Chromium Pickle holding a JSON directory, laid out as four
    little-endian uint32 fields before the JSON itself::

        @0   4            pickle size of the next field
        @4   header_size  size of the header block
        @8   json_pickle  size of the JSON pickle
        @12  json_len     length of the JSON string
        @16  ...          the JSON directory

    File bodies begin at ``8 + header_size``. Reading json_len from the wrong
    field yields a truncated slice and a JSON decode error, which is how this
    first went wrong.
    """
    try:
        with open(path, "rb") as fdesc:
            prefix = fdesc.read(16)
            if len(prefix) < 16:
                return None
            header_size = int.from_bytes(prefix[4:8], "little")
            json_len = int.from_bytes(prefix[12:16], "little")
            if not 0 < json_len <= header_size < 256 * 1024 * 1024:
                return None
            raw = fdesc.read(json_len)
            if len(raw) < json_len:
                return None
            directory = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(directory, dict) or "files" not in directory:
        return None
    return directory, 8 + header_size


def _asar_walk(node: dict, prefix: str = "") -> list[tuple[str, dict]]:
    """Flatten an asar directory tree into (path, entry) pairs."""
    found: list[tuple[str, dict]] = []
    files = node.get("files")
    if not isinstance(files, dict):
        return found
    for name, entry in files.items():
        if not isinstance(entry, dict):
            continue
        full = f"{prefix}/{name}" if prefix else name
        if "files" in entry:
            found.extend(_asar_walk(entry, full))
        else:
            found.append((full, entry))
    return found


def extract_asar_icon(asar_path: str, destination: str) -> Optional[str]:
    """Pull the most icon-like PNG out of an asar archive.

    Prefers conventional icon filenames, then falls back to the largest PNG in
    the archive, which in practice is the application icon.
    """
    parsed = _asar_header(asar_path)
    if parsed is None:
        return None
    directory, data_offset = parsed

    pngs = [
        (path, entry)
        for path, entry in _asar_walk(directory)
        if path.lower().endswith(".png")
        and isinstance(entry.get("size"), int)
        and entry.get("offset") is not None
    ]
    if not pngs:
        return None

    def rank(item: tuple[str, dict]) -> tuple[int, int]:
        path, entry = item
        name = os.path.basename(path).lower()
        named = len(_ASAR_ICON_NAMES) - _ASAR_ICON_NAMES.index(name) if name in _ASAR_ICON_NAMES else 0
        return (named, entry["size"])

    path, entry = max(pngs, key=rank)

    try:
        offset = data_offset + int(entry["offset"])
        with open(asar_path, "rb") as fdesc:
            fdesc.seek(offset)
            blob = fdesc.read(int(entry["size"]))
        if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as fdesc:
            fdesc.write(blob)
    except (OSError, ValueError, KeyError):
        return None
    return destination
