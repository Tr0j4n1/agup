"""Command line interface."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

from .endpoints import COMPONENTS, ConfigError, Endpoints
from .integrity import PinStore
from .outcome import EXIT_USAGE, RunReport, Status
from .update import Options, update_bundle, update_cli

__version__ = "1.0.0"

_MARK = {
    Status.UPDATED: "+",
    Status.CURRENT: "=",
    Status.SKIPPED: "~",
    Status.FAILED: "!",
}


def default_config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "agup", "config.toml")


def load_config(path: Optional[str]) -> dict[str, Any]:
    """Read the TOML config, if present."""
    resolved = path or os.environ.get("AGUP_CONFIG") or default_config_path()
    if not os.path.isfile(resolved):
        if path:
            raise ConfigError(f"Config file not found: {resolved}")
        return {}
    if tomllib is None:
        raise ConfigError("TOML config requires Python 3.11 or newer")
    try:
        with open(resolved, "rb") as fdesc:
            return tomllib.load(fdesc)
    except (OSError, ValueError) as err:
        raise ConfigError(f"Cannot read {resolved}: {err}") from err


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agup",
        description="Update Antigravity IDE, Hub and CLI outside the vendor's package feed.",
        epilog=(
            "Exit codes: 0 everything current or updated, 1 something failed, "
            "2 nothing failed but something was skipped, 3 bad usage."
        ),
    )
    parser.add_argument("--version", action="version", version=f"agup {__version__}")

    select = parser.add_argument_group("component selection")
    for component in COMPONENTS:
        select.add_argument(
            f"--{component}", action="store_true", help=f"act on the {component} only"
        )

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("-n", "--dry-run", action="store_true", help="report without installing")
    behaviour.add_argument("--force", action="store_true", help="ignore version and process checks")
    behaviour.add_argument("--no-link", action="store_true", help="skip creating command symlinks")
    behaviour.add_argument(
        "--no-desktop", action="store_true", help="skip installing application menu entries"
    )
    behaviour.add_argument(
        "--migrate-profile",
        action="store_true",
        help="copy settings and history from a previous profile if the data folder changed",
    )
    behaviour.add_argument(
        "--migrate-extensions",
        action="store_true",
        help="with --migrate-profile, also copy extensions (large, and re-downloadable)",
    )
    behaviour.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="download progress bar; auto shows it only on a terminal",
    )
    behaviour.add_argument("--dir-ide", metavar="PATH", help="override the IDE install directory")
    behaviour.add_argument("--dir-hub", metavar="PATH", help="override the Hub install directory")
    behaviour.add_argument("--path-cli", metavar="PATH", help="override the agy binary path")
    behaviour.add_argument(
        "--adopt-managed-path",
        action="store_true",
        help="allow writing over a directory owned by a Debian package (desyncs dpkg)",
    )
    behaviour.add_argument(
        "--scope",
        choices=("user", "system"),
        default=os.environ.get("AGUP_SCOPE", "user"),
        help="install under ~/opt (user) or /opt (system, needs root)",
    )

    integrity = parser.add_argument_group("integrity")
    integrity.add_argument(
        "--strict",
        action="store_true",
        help="refuse artifacts with neither a server checksum nor a recorded pin",
    )
    integrity.add_argument("--pin-store", metavar="PATH", help="override the pin store location")
    integrity.add_argument("--list-pins", action="store_true", help="print recorded pins and exit")
    integrity.add_argument(
        "--forget-pin",
        metavar="COMPONENT:VERSION",
        help="drop a recorded pin so the next download is re-pinned",
    )

    net = parser.add_argument_group("endpoints")
    for component in COMPONENTS:
        net.add_argument(
            f"--{component}-endpoint",
            metavar="URL",
            help=f"override the {component} endpoint (env: AGUP_{component.upper()}_ENDPOINT)",
        )
    net.add_argument("--project", help="override the Cloud Run project number")
    net.add_argument("--region", help="override the Cloud Run region")
    net.add_argument(
        "--allow-insecure-endpoint",
        action="store_true",
        help="permit a plain-HTTP endpoint (local mirrors only)",
    )
    net.add_argument("--show-endpoints", action="store_true", help="print endpoints and exit")

    parser.add_argument("--config", metavar="PATH", help="TOML config file")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    def report(text: str) -> None:
        if not args.quiet:
            print(text)

    try:
        config = load_config(args.config)
        endpoints = Endpoints.resolve(
            overrides={
                component: getattr(args, f"{component}_endpoint")
                for component in COMPONENTS
                if getattr(args, f"{component}_endpoint")
            },
            toml_config=config,
            project=args.project,
            region=args.region,
            allow_insecure=args.allow_insecure_endpoint,
        )
    except ConfigError as err:
        print(f"agup: {err}", file=sys.stderr)
        return EXIT_USAGE

    store = PinStore(args.pin_store)

    if args.show_endpoints:
        print("release feeds:")
        for name, url in endpoints.describe():
            print(f"  {name:4} {url}")
        print("artifact templates:")
        for name, template in endpoints.describe_artifacts():
            print(f"  {name:4} {template}")
        return 0

    if args.list_pins:
        pins = store.entries()
        if not pins:
            print(f"No pins recorded ({store.path})")
        for key, digest in sorted(pins.items()):
            print(f"{key:24} {digest}")
        return 0

    if args.forget_pin:
        if ":" not in args.forget_pin:
            print("agup: --forget-pin needs COMPONENT:VERSION", file=sys.stderr)
            return EXIT_USAGE
        component, version = args.forget_pin.split(":", 1)
        if store.forget(component, version):
            print(f"Dropped pin for {component} {version}")
            return 0
        print(f"No pin recorded for {component} {version}", file=sys.stderr)
        return EXIT_USAGE

    if args.scope == "system" and os.geteuid() != 0:
        print("agup: --scope system needs root; re-run under sudo", file=sys.stderr)
        return EXIT_USAGE

    selected = [c for c in COMPONENTS if getattr(args, c)] or list(COMPONENTS)

    paths_cfg = config.get("paths", {}) or {}
    options = Options(
        scope=args.scope,
        dir_ide=args.dir_ide or os.environ.get("AGUP_DIR_IDE") or paths_cfg.get("ide"),
        dir_hub=args.dir_hub or os.environ.get("AGUP_DIR_HUB") or paths_cfg.get("hub"),
        path_cli=args.path_cli or os.environ.get("AGUP_PATH_CLI") or paths_cfg.get("cli"),
        adopt_managed=args.adopt_managed_path,
        show_progress=(
            None if args.progress == "auto" and not args.quiet
            else False if args.quiet or args.progress == "never"
            else True
        ),
        force=args.force,
        dry_run=args.dry_run,
        strict=args.strict,
        pin_store=store,
        link=not args.no_link,
        desktop=not args.no_desktop,
        migrate_profile=args.migrate_profile,
        migrate_extensions=args.migrate_extensions,
    )

    report(f"agup {__version__} — scope {args.scope}" + (" — dry run" if args.dry_run else ""))

    run = RunReport()
    for component in selected:
        report(f"\n{component}:")
        if component == "cli":
            run.add(update_cli(endpoints, options, report))
        else:
            run.add(update_bundle(component, endpoints, options, report))

    report("")
    for outcome in run.outcomes:
        print(f"{_MARK[outcome.status]} {outcome.component:4} {outcome.detail}")
    print(f"\n{run.summary_line()}")

    return run.exit_code()


def entrypoint() -> None:
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    entrypoint()
