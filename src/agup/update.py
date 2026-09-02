"""Update orchestration.

One function per component, each returning an Outcome rather than a boolean, so
the caller can tell 'the Hub was open so I left it alone' apart from 'the Hub
failed its checksum'.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

from .desktop import install_desktop_entry
from .migrate import (
    describe,
    find_predecessor,
    migrate,
    profile_for,
    read_app_name,
    read_data_folder_name,
)
from .endpoints import ConfigError, Endpoints
from .fetch import FetchError, ProgressBar, download, get_json
from .install import (
    InstallError,
    is_dpkg_owned,
    read_product_identity,
    Paths,
    extract_tarball,
    find_payload_root,
    is_writable,
    link_command,
    read_binary_version,
    read_installed_version,
    write_receipt,
    running_pids,
    swap_directory,
)
from .integrity import IntegrityError, PinStore, verify_artifact
from .outcome import Outcome
from .versions import Release, UNKNOWN_VERSION, is_newer, select_latest

Reporter = Callable[[str], None]

_PROCESS_PATTERNS = {
    "ide": ("Antigravity-IDE", "antigravity-ide"),
    "hub": ("Antigravity/antigravity", "antigravity-hub"),
    "cli": ("agy",),
}

_PAYLOAD_HINTS = {
    "ide": ("Antigravity IDE", "Antigravity-IDE", "AntigravityIDE"),
    "hub": ("Antigravity", "Antigravity-x64"),
}


@dataclass
class Options:
    """Everything that changes how an update behaves."""

    scope: str = "user"
    dir_ide: Optional[str] = None
    dir_hub: Optional[str] = None
    path_cli: Optional[str] = None
    adopt_managed: bool = False
    force: bool = False
    dry_run: bool = False
    strict: bool = False
    pin_store: Optional[PinStore] = None
    link: bool = True
    desktop: bool = True
    migrate_profile: bool = False
    migrate_extensions: bool = False
    show_progress: Optional[bool] = None

    def store(self) -> PinStore:
        return self.pin_store or PinStore()

    def progress_for(self, label: str) -> Optional[ProgressBar]:
        """A progress bar for a download, or None when output is suppressed."""
        if self.show_progress is False:
            return None
        return ProgressBar(label, force=True if self.show_progress else None)

    def paths(self) -> Paths:
        return Paths.for_scope(self.scope).with_overrides(
            ide_dir=self.dir_ide, hub_dir=self.dir_hub, cli_bin=self.path_cli
        )


def _fetch_releases(endpoint: str) -> list[Release]:
    payload = get_json(endpoint)
    if isinstance(payload, dict):
        payload = payload.get("releases") or payload.get("versions") or [payload]
    if not isinstance(payload, list):
        raise FetchError(f"{endpoint} returned an unexpected shape")

    releases: list[Release] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            releases.append(Release.from_payload(item))
        except ValueError:
            continue
    if not releases:
        raise FetchError(f"{endpoint} returned no usable releases")
    return releases


def _preflight(
    component: str,
    target: str,
    options: Options,
    report: Reporter,
) -> Optional[Outcome]:
    """Checks that should stop us before spending bandwidth."""
    pids = running_pids(*_PROCESS_PATTERNS.get(component, ()))
    if pids and not options.force:
        return Outcome.skipped(
            component, f"process running (PID {', '.join(pids)}); close it or pass --force"
        )
    if pids:
        report(f"  {component} is running (PID {', '.join(pids)}); continuing due to --force")

    if not is_writable(target):
        hint = "run with sudo" if options.scope == "system" else "check directory ownership"
        return Outcome.skipped(component, f"{target} is not writable; {hint}")

    if component in ("ide", "hub") and os.path.exists(target):
        identity = read_product_identity(target)
        name = (identity.get("nameLong") or "").lower()
        if name and component == "hub" and "hub" not in name:
            return Outcome.skipped(
                component,
                f"{target} looks like {identity.get('nameLong')}, not the Hub; "
                f"pass --dir-hub to point elsewhere",
            )

    if os.path.exists(target) and is_dpkg_owned(target) and not options.adopt_managed:
        return Outcome.skipped(
            component,
            f"{target} is owned by a Debian package. Overwriting it desyncs dpkg "
            f"and the next apt upgrade will conflict. Either 'apt remove' the "
            f"package first, or pass --adopt-managed-path to proceed anyway",
        )

    return None


def _find_executable(install_dir: str, component: str) -> Optional[str]:
    """Locate the launcher inside an installed bundle.

    Release tarballs and distro packages disagree on the binary name -- the
    Debian package ships ``antigravity`` where the tarball ships
    ``antigravity-ide`` -- so several names are tried before giving up.
    """
    names = (
        ["antigravity-ide", "antigravity"]
        if component == "ide"
        else ["antigravity", "antigravity-hub"]
    )
    for name in names:
        for candidate in (
            os.path.join(install_dir, name),
            os.path.join(install_dir, "bin", name),
        ):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def update_bundle(
    component: str,
    endpoints: Endpoints,
    options: Options,
    report: Reporter,
) -> Outcome:
    """Update an Electron bundle component (ide or hub)."""
    paths = options.paths()
    target = paths.for_component(component)

    try:
        releases = _fetch_releases(endpoints.for_component(component))
    except FetchError as err:
        return Outcome.failed(component, str(err))

    latest = select_latest(releases)
    if latest is None:
        return Outcome.failed(component, "no releases returned")

    installed = read_installed_version(target)
    report(f"  {component}: installed {installed}, available {latest.version}")

    if not options.force and not is_newer(latest.version, installed):
        return Outcome.current(component, installed)

    if options.dry_run:
        return Outcome.skipped(component, f"update to {latest.version} available (dry run)")

    blocked = _preflight(component, target, options, report)
    if blocked:
        return blocked

    try:
        download_url = latest.download_url or endpoints.artifact_url(
            component, latest.version, latest.execution_id or ""
        )
    except ConfigError as err:
        return Outcome.failed(component, str(err))

    with tempfile.TemporaryDirectory(prefix="agup-") as workdir:
        archive = os.path.join(workdir, f"{component}.tar.gz")
        try:
            download(
                download_url,
                archive,
                label=f"  {component} {latest.version}",
                progress=options.progress_for(f"  {component} {latest.version}"),
            )
        except FetchError as err:
            return Outcome.failed(component, str(err))

        try:
            result = verify_artifact(
                archive,
                component,
                latest.version,
                sha512=latest.sha512,
                sha256=latest.sha256,
                store=options.store(),
                strict=options.strict,
            )
        except IntegrityError as err:
            return Outcome.failed(component, str(err))
        report(f"  {result.message}")

        extract_dir = os.path.join(workdir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        try:
            extract_tarball(archive, extract_dir)
            payload = find_payload_root(extract_dir, _PAYLOAD_HINTS[component])
            swap_directory(payload, target)
        except InstallError as err:
            return Outcome.failed(component, str(err))

    write_receipt(target, component, latest.version)

    binary = _find_executable(target, component)
    if binary is None:
        report(f"  warning: no launcher binary found under {target}")
    else:
        if options.link:
            link_name = "antigravity" if component == "hub" else "antigravity-ide"
            try:
                link_command(binary, os.path.join(paths.bin_dir, link_name))
            except InstallError as err:
                report(f"  warning: {err}")

        if options.desktop:
            entry, icon = install_desktop_entry(
                component, target, binary, scope=options.scope, version=latest.version
            )
            if entry:
                report(f"  menu entry: {entry}")
                if not icon:
                    report(
                        "  warning: no icon found in the bundle; the menu entry "
                        "will show a generic icon"
                    )
            else:
                report("  warning: could not write a desktop entry")

    _check_profile(component, target, installed, latest.version, options, report)

    return Outcome.updated(component, installed, latest.version)


def _check_profile(
    component: str,
    install_dir: str,
    previous_version: str,
    new_version: str,
    options: Options,
    report: Reporter,
) -> None:
    """Warn when the new build uses a different data folder, and optionally migrate.

    A renamed data folder makes a new version look, on first launch, like every
    setting and conversation has been lost -- while the old profile sits intact
    under its old name. Saying so at install time costs one line; discovering it
    afterwards costs an afternoon.
    """
    folder = read_data_folder_name(install_dir)
    app_name = read_app_name(install_dir)
    if not folder or not app_name:
        return

    previous = find_predecessor(folder, app_name)
    if previous is None:
        return

    destination = profile_for(folder, app_name)
    report(f"  note: {new_version} uses a new profile directory ({folder}).")
    report(f"        Your previous data is still at {describe(previous)}")

    if not options.migrate_profile:
        report("        Nothing was copied. Re-run with --migrate-profile to carry it across.")
        return

    report("  migrating previous profile...")
    moved = migrate(
        previous,
        destination,
        include_extensions=options.migrate_extensions,
        backup=True,
    )
    report(
        f"  migrated {moved['config_files']} settings/history files"
        + (f" and {moved['data_files']} extension files" if options.migrate_extensions else "")
    )
    report(f"        original left untouched at {previous.config_dir}")


#: Names the CLI binary has shipped under. The archive currently contains a
#: single file called "antigravity"; earlier tooling assumed "agy".
_CLI_NAMES = ("antigravity", "agy", "antigravity-cli")


def _find_cli_binary(extract_dir: str) -> Optional[str]:
    """Locate the CLI executable in an extracted archive.

    Falls back to "the only executable file present" rather than failing on an
    unrecognised name, since the archive is a single binary and guessing its
    name wrong is how this broke before.
    """
    candidates: list[str] = []
    for root, _dirs, files in os.walk(extract_dir):
        for name in files:
            path = os.path.join(root, name)
            if name in _CLI_NAMES:
                return path
            if os.access(path, os.X_OK) and not os.path.islink(path):
                candidates.append(path)

    if len(candidates) == 1:
        return candidates[0]
    return None


def update_cli(endpoints: Endpoints, options: Options, report: Reporter) -> Outcome:
    """Update the agy CLI binary."""
    paths = options.paths()
    target = paths.cli_bin
    manifest_url = f"{endpoints.cli}/linux_amd64.json"

    try:
        payload = get_json(manifest_url)
    except FetchError as err:
        return Outcome.failed("cli", str(err))

    if not isinstance(payload, dict):
        return Outcome.failed("cli", f"{manifest_url} returned an unexpected shape")

    try:
        latest = Release.from_payload(payload)
    except ValueError as err:
        return Outcome.failed("cli", f"unusable manifest: {err}")

    installed = read_binary_version(target)
    report(f"  cli: installed {installed}, available {latest.version}")

    if not options.force and installed != UNKNOWN_VERSION and not is_newer(latest.version, installed):
        return Outcome.current("cli", installed)

    if options.dry_run:
        return Outcome.skipped("cli", f"update to {latest.version} available (dry run)")

    blocked = _preflight("cli", target, options, report)
    if blocked:
        return blocked

    with tempfile.TemporaryDirectory(prefix="agup-cli-") as workdir:
        archive = os.path.join(workdir, "cli.tar.gz")
        cli_url = latest.download_url
        if not cli_url:
            return Outcome.failed("cli", "manifest carried no download URL")
        try:
            download(
                cli_url,
                archive,
                label=f"  cli {latest.version}",
                progress=options.progress_for(f"  cli {latest.version}"),
            )
        except FetchError as err:
            return Outcome.failed("cli", str(err))

        try:
            result = verify_artifact(
                archive,
                "cli",
                latest.version,
                sha512=latest.sha512,
                sha256=latest.sha256,
                store=options.store(),
                strict=options.strict,
            )
        except IntegrityError as err:
            return Outcome.failed("cli", str(err))
        report(f"  {result.message}")

        extract_dir = os.path.join(workdir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        try:
            extract_tarball(archive, extract_dir)
        except InstallError as err:
            return Outcome.failed("cli", str(err))

        binary = _find_cli_binary(extract_dir)
        if binary is None:
            listing = sorted(
                name
                for _root, _dirs, files in os.walk(extract_dir)
                for name in files
            )[:10]
            return Outcome.failed(
                "cli",
                f"archive contained no recognisable CLI binary; found {listing}",
            )

        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            swap_directory(binary, target)
            os.chmod(target, 0o755)
        except (InstallError, OSError) as err:
            return Outcome.failed("cli", f"cannot install agy: {err}")

    return Outcome.updated("cli", installed, latest.version)
