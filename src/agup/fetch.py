"""HTTP fetching.

Kept deliberately small: a JSON getter and a streaming downloader, both with
bounded retries on transient failures, and a hard size cap so a malformed or
hostile content-length cannot fill the disk.
"""

from __future__ import annotations

import json
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from .endpoints import is_trusted_download

USER_AGENT = "agup"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class FetchError(Exception):
    """Raised when a resource cannot be retrieved."""


class UntrustedDownloadError(FetchError):
    """Raised when a download URL is outside the permitted host set."""


def _backoff(attempt: int) -> float:
    return min(0.5 * (2**attempt) + random.uniform(0.05, 0.25), 5.0)


def _request(url: str, timeout: int):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310 - scheme validated upstream


def get_json(url: str, *, timeout: int = 15, retries: int = 3) -> Any:
    """GET a URL and parse the response as JSON."""
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            with _request(url, timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            last = err
            if err.code in _RETRYABLE_STATUS and attempt < retries:
                time.sleep(_backoff(attempt))
                continue
            raise FetchError(f"{url} returned HTTP {err.code}") from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            last = err
            if attempt < retries:
                time.sleep(_backoff(attempt))
                continue
            reason = getattr(err, "reason", err)
            raise FetchError(f"Cannot reach {url}: {reason}") from err
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            raise FetchError(f"{url} did not return valid JSON") from err
    raise FetchError(f"Failed to fetch {url}: {last}")


def format_bytes(count: float) -> str:
    """Human-readable byte count."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(count) < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} GB"


def format_duration(seconds: float) -> str:
    """Compact duration for an ETA."""
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class ProgressBar:
    """Terminal progress bar for a single download.

    Redraws are throttled and only emitted to a TTY, so piping output or
    running under a systemd timer produces a couple of clean lines rather than
    thousands of carriage returns in the journal.
    """

    FILLED = "\u2501"
    EMPTY = "\u2500"

    def __init__(
        self,
        label: str,
        *,
        stream=None,
        width: int = 28,
        interval: float = 0.1,
        force: Optional[bool] = None,
    ) -> None:
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self.width = width
        self.interval = interval
        self.enabled = force if force is not None else self._is_tty()
        self.started = time.monotonic()
        self._last_draw = 0.0
        self._last_len = 0
        self._final = False

    def _is_tty(self) -> bool:
        try:
            return bool(self.stream.isatty())
        except (AttributeError, ValueError):
            return False

    #: Below this many seconds elapsed, rate and ETA are noise.
    WARMUP = 0.4

    def _render(self, done: int, total: int) -> str:
        elapsed = max(time.monotonic() - self.started, 1e-6)
        warm = elapsed >= self.WARMUP
        rate = done / elapsed if warm else 0.0
        rate_text = f"{format_bytes(rate)}/s" if warm else "--"

        if total > 0:
            fraction = min(done / total, 1.0)
            filled = int(self.width * fraction)
            bar = self.FILLED * filled + self.EMPTY * (self.width - filled)
            eta = (total - done) / rate if rate > 0 else float("inf")
            return (
                f"{self.label} {bar} {fraction * 100:5.1f}%  "
                f"{format_bytes(done)}/{format_bytes(total)}  "
                f"{rate_text}  ETA {format_duration(eta) if warm else '--:--'}"
            )

        # No content-length: show what we have rather than a fake percentage.
        spin = "|/-\\"[int(elapsed * 8) % 4]
        return f"{self.label} {spin}  {format_bytes(done)}  {rate_text}"

    def update(self, done: int, total: int) -> None:
        """Redraw, subject to the throttle interval."""
        if not self.enabled or self._final:
            return
        now = time.monotonic()
        complete = total > 0 and done >= total
        if not complete and now - self._last_draw < self.interval:
            return
        self._last_draw = now
        self._write(self._render(done, total))

    def _write(self, text: str) -> None:
        padding = " " * max(self._last_len - len(text), 0)
        self.stream.write(f"\r{text}{padding}")
        self.stream.flush()
        self._last_len = len(text)

    def finish(self, done: int, total: int, *, keep: bool = True) -> None:
        """Draw the final state and end the line."""
        if self._final:
            return
        self._final = True
        if not self.enabled:
            return
        if keep:
            elapsed = max(time.monotonic() - self.started, 1e-6)
            self._write(
                f"{self.label} {self.FILLED * self.width} "
                f"{format_bytes(done)} in {format_duration(elapsed)} "
                f"({format_bytes(done / elapsed)}/s)"
            )
            self.stream.write("\n")
        else:
            self.stream.write("\r" + " " * self._last_len + "\r")
        self.stream.flush()

    def abort(self) -> None:
        """Clear the bar without claiming success."""
        self.finish(0, 0, keep=False)

    def restart(self) -> "ProgressBar":
        """A fresh bar for a retried attempt, so rate and ETA are not skewed."""
        return ProgressBar(
            self.label,
            stream=self.stream,
            width=self.width,
            interval=self.interval,
            force=self.enabled,
        )


def download(
    url: str,
    dest_path: str,
    *,
    label: str = "Downloading",
    max_bytes: int = DEFAULT_MAX_BYTES,
    retries: int = 3,
    timeout: int = 60,
    progress: Optional["ProgressBar"] = None,
    enforce_trusted_host: bool = True,
) -> int:
    """Stream a URL to disk, returning the byte count."""
    if enforce_trusted_host and not is_trusted_download(url):
        raise UntrustedDownloadError(
            f"Refusing to download from {url!r}: not an approved Google download host."
        )

    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            with _request(url, timeout) as response:
                declared = int(response.headers.get("content-length") or 0)
                if declared > max_bytes:
                    raise FetchError(
                        f"Refusing download: content-length {declared} exceeds cap {max_bytes}"
                    )
                written = 0
                try:
                    with open(dest_path, "wb") as fdesc:
                        while chunk := response.read(65536):
                            written += len(chunk)
                            if written > max_bytes:
                                raise FetchError(
                                    f"Refusing download: exceeded cap of {max_bytes} bytes"
                                )
                            fdesc.write(chunk)
                            if progress is not None:
                                progress.update(written, declared)
                except BaseException:
                    # Covers KeyboardInterrupt too: leave the terminal usable.
                    if progress is not None:
                        progress.abort()
                    raise
                if progress is not None:
                    progress.finish(written, declared)
                return written
        except urllib.error.HTTPError as err:
            last = err
            if progress is not None:
                progress.abort()
            if err.code in _RETRYABLE_STATUS and attempt < retries:
                time.sleep(_backoff(attempt))
                progress = progress.restart() if progress is not None else None
                continue
            raise FetchError(f"Download of {url} failed: HTTP {err.code}") from err
        except (urllib.error.URLError, TimeoutError) as err:
            last = err
            if progress is not None:
                progress.abort()
            if attempt < retries:
                time.sleep(_backoff(attempt))
                progress = progress.restart() if progress is not None else None
                continue
            raise FetchError(f"Download of {url} failed: {getattr(err, 'reason', err)}") from err
        except OSError as err:
            if progress is not None:
                progress.abort()
            raise FetchError(f"Cannot write to {dest_path}: {err}") from err
    raise FetchError(f"Download of {url} failed after {retries} retries: {last}")
