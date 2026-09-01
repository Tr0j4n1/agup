"""Endpoint resolution.

Every URL this tool talks to is overridable. Upstream hardcodes three Cloud Run
hostnames in a constants module, which means when those names stop resolving --
DNS filtering, a torn-down backend, an air-gapped mirror -- there is no recourse
short of editing installed source.

Precedence, highest first: CLI flag, environment variable, TOML config, default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

DEFAULT_PROJECT = "974169037036"
DEFAULT_REGION = "us-central1"

#: Components this tool knows how to manage.
COMPONENTS = ("ide", "hub", "cli")

#: Hosts a release archive is permitted to be served from. The release metadata
#: endpoint hands us a download URL; we do not follow it anywhere else.
TRUSTED_DOWNLOAD_SUFFIXES = (
    ".google.com",
    ".googleapis.com",
    ".googleusercontent.com",
    ".gvt1.com",
)
TRUSTED_DOWNLOAD_HOSTS = frozenset(
    {"dl.google.com", "storage.googleapis.com", "edgedl.me.gvt1.com"}
)


class ConfigError(ValueError):
    """Raised when supplied configuration cannot be used."""


#: Artifact URL templates. The release feed returns only a version and an
#: execution id; the download URL is constructed from them. Placeholders:
#: {version}, {exec_id}, {os}, {arch}.
DEFAULT_ARTIFACT_TEMPLATES = {
    "ide": (
        "https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/"
        "{version}-{exec_id}/{os}-{arch}/Antigravity%20IDE.tar.gz"
    ),
    "hub": (
        "https://storage.googleapis.com/antigravity-public/antigravity-hub/"
        "{version}-{exec_id}/{os}-{arch}/Antigravity.tar.gz"
    ),
}


def _default_endpoint(component: str, project: str, region: str) -> str:
    base = f"https://antigravity-{component}-auto-updater-{project}.{region}.run.app"
    if component == "cli":
        return f"{base}/manifests"
    return f"{base}/releases"


def _env_name(component: str) -> str:
    return f"AGUP_{component.upper()}_ENDPOINT"


def validate_endpoint(url: str, *, allow_insecure: bool = False) -> str:
    """Reject anything that is not a usable absolute URL.

    Plain HTTP is refused unless explicitly permitted, which exists for
    pointing at a local mirror during testing and nothing else.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(f"Endpoint must be http(s): {url!r}")
    if not parsed.netloc:
        raise ConfigError(f"Endpoint has no host: {url!r}")
    if parsed.scheme == "http" and not allow_insecure:
        raise ConfigError(
            f"Refusing plain HTTP endpoint {url!r}. Pass --allow-insecure-endpoint "
            "if this is a deliberate local mirror."
        )
    return url.rstrip("/")


def is_trusted_download(url: str) -> bool:
    """Whether a download URL points somewhere we are willing to fetch from."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in TRUSTED_DOWNLOAD_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in TRUSTED_DOWNLOAD_SUFFIXES)


@dataclass
class Endpoints:
    """Resolved endpoint for each component."""

    ide: str
    hub: str
    cli: str
    artifacts: dict[str, str] = field(default_factory=dict)

    def for_component(self, component: str) -> str:
        try:
            return getattr(self, component)
        except AttributeError as err:
            raise ConfigError(f"Unknown component {component!r}") from err

    def artifact_url(
        self,
        component: str,
        version: str,
        exec_id: str,
        *,
        os_name: str = "linux",
        arch: str = "x64",
    ) -> str:
        """Build the download URL for a release.

        The release feed returns a version and an execution id but no URL, so
        the artifact location has to be constructed. The template is
        overridable because the CDN layout is the vendor's to change.
        """
        template = self.artifacts.get(component) or DEFAULT_ARTIFACT_TEMPLATES.get(component)
        if not template:
            raise ConfigError(f"No artifact template for component {component!r}")
        if not exec_id:
            raise ConfigError(f"Release {version} of {component} has no execution id")
        try:
            return template.format(
                version=version, exec_id=exec_id, os=os_name, arch=arch
            )
        except KeyError as err:
            raise ConfigError(
                f"Artifact template for {component} uses unknown placeholder {err}"
            ) from err

    def describe(self) -> list[tuple[str, str]]:
        return [(name, self.for_component(name)) for name in COMPONENTS]

    def describe_artifacts(self) -> list[tuple[str, str]]:
        return [
            (name, self.artifacts.get(name) or DEFAULT_ARTIFACT_TEMPLATES[name])
            for name in DEFAULT_ARTIFACT_TEMPLATES
        ]

    @classmethod
    def resolve(
        cls,
        *,
        overrides: Optional[dict[str, str]] = None,
        toml_config: Optional[dict[str, Any]] = None,
        env: Optional[dict[str, str]] = None,
        project: Optional[str] = None,
        region: Optional[str] = None,
        allow_insecure: bool = False,
    ) -> "Endpoints":
        """Build endpoints from every configuration layer."""
        env = os.environ if env is None else env
        overrides = overrides or {}
        toml_endpoints = (toml_config or {}).get("endpoints", {}) or {}

        project = (
            project
            or env.get("AGUP_PROJECT")
            or (toml_config or {}).get("project")
            or DEFAULT_PROJECT
        )
        region = (
            region
            or env.get("AGUP_REGION")
            or (toml_config or {}).get("region")
            or DEFAULT_REGION
        )

        resolved: dict[str, str] = {}
        for component in COMPONENTS:
            value = (
                overrides.get(component)
                or env.get(_env_name(component))
                or toml_endpoints.get(component)
                or _default_endpoint(component, project, region)
            )
            resolved[component] = validate_endpoint(value, allow_insecure=allow_insecure)

        artifacts: dict[str, str] = {}
        toml_artifacts = (toml_config or {}).get("artifacts", {}) or {}
        for component in DEFAULT_ARTIFACT_TEMPLATES:
            value = (
                (overrides or {}).get(f"{component}_artifact")
                or env.get(f"AGUP_{component.upper()}_ARTIFACT")
                or toml_artifacts.get(component)
            )
            if value:
                artifacts[component] = value

        return cls(**resolved, artifacts=artifacts)
