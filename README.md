# agup

Updater for Antigravity IDE, Hub and CLI.

Google ships Antigravity through an apt repo that lags well behind the release
feed the applications' own auto-updaters query. If you install from that repo
you sit on an old build with no way to move forward. `agup` talks to the release
feed directly.

Not affiliated with Google.

## Install

```bash
git clone <your-repo> ~/agup
cd ~/agup
pipx install .
```

Or run without installing:

```bash
python3 -m agup --help
```

Needs Python 3.11+ and no third-party packages. Standard library only, so there
is no dependency tree to audit and nothing to break on a rolling distro.

## Usage

```bash
agup                      # update everything, user scope
agup --ide                # one component
agup -n                   # dry run: report, change nothing
agup --scope system       # install to /opt (needs sudo)
agup --show-endpoints     # print what it will talk to
```

## Progress

Downloads show a bar with transfer rate and ETA:

```
  ide 2.5.5 ━━━━━━━━━━━━━━───────────────  49.6%  108.7 MB/219.1 MB  11.4 MB/s  ETA 00:09
```

It draws to stderr, so stdout stays clean for piping, and it is suppressed
automatically when stderr is not a terminal -- a systemd timer gets two lines
in the journal, not ten thousand carriage returns. `--progress always` forces
it on, `--progress never` off, and `--quiet` implies never.

Rate and ETA are withheld for the first fraction of a second, since a rate
computed over a few milliseconds is noise. An interrupted or retried download
clears its bar rather than leaving a half-drawn line on your terminal.

## Exit codes

The reason this exists as its own tool. A component that was deliberately left
alone is not a failure:

| Code | Meaning |
|------|---------|
| 0 | everything updated or already current |
| 1 | something genuinely failed |
| 2 | nothing failed, something was skipped (app running, path not writable) |
| 3 | bad usage or unusable config |

So an unattended run can treat 0 and 2 as fine and alert only on 1:

```ini
# ~/.config/systemd/user/agup.service
[Unit]
Description=Update Antigravity components

[Service]
Type=oneshot
ExecStart=%h/.local/bin/agup --quiet
SuccessExitStatus=0 2
```

```ini
# ~/.config/systemd/user/agup.timer
[Unit]
Description=Weekly Antigravity update check

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now agup.timer
```

## Endpoints

Nothing is hardcoded past the default. Precedence, highest first:

1. `--ide-endpoint` / `--hub-endpoint` / `--cli-endpoint`
2. `AGUP_IDE_ENDPOINT` / `AGUP_HUB_ENDPOINT` / `AGUP_CLI_ENDPOINT`
3. `~/.config/agup/config.toml`
4. built-in defaults

```toml
# ~/.config/agup/config.toml
project = "974169037036"
region  = "us-central1"

[endpoints]
ide = "https://mirror.internal/antigravity/ide/releases"

[artifacts]
ide = "https://mirror.internal/ide/{version}-{exec_id}/{os}-{arch}/Antigravity%20IDE.tar.gz"
```

The IDE and Hub release feeds return only a version and an execution id -- no
download URL. The artifact location is constructed from a template, which is
separately overridable via `[artifacts]` or `AGUP_IDE_ARTIFACT` /
`AGUP_HUB_ARTIFACT`. Placeholders: `{version}`, `{exec_id}`, `{os}`, `{arch}`.
The CLI manifest carries a URL directly and needs no template.

`agup --show-endpoints` prints both the feeds and the templates.

This matters more than it sounds. Those defaults are Cloud Run hostnames under
`run.app`, which appears on several DNS blocklists and is filtered outright by
some consumer routers. When resolution fails there is otherwise no recourse
short of editing installed source.

Plain HTTP endpoints are refused unless you pass `--allow-insecure-endpoint`,
which exists for local mirrors and nothing else.

## Integrity

Download URLs are constrained to Google-operated hosts over HTTPS regardless of
what the release feed returns, so a compromised feed cannot redirect the
download somewhere arbitrary.

Archives are verified in three tiers:

1. **Server checksum.** The release carries a sha512 or sha256. Verified
   against it; a mismatch aborts.
2. **Recorded pin.** No server checksum, but this exact version was installed
   before. Compared against the digest recorded then. A mismatch aborts.
3. **First sight.** No server checksum and a version never seen. The digest is
   recorded and installation proceeds, saying so plainly.

Tier 3 does not authenticate anything and this is not pretended otherwise. The
vendor publishes no signature, so a first download cannot be verified by any
means available here. What the pin store buys is that the unverified moment
happens *once per version* rather than on every run, and that an artifact
silently changing under an already-installed version becomes a hard failure.

`--strict` removes tier 3, refusing anything unverifiable. Good for automation,
though it means a genuinely new release needs one non-strict run to pin.

```bash
agup --list-pins
agup --forget-pin ide:2.5.5     # allow a legitimate respin to re-pin
```

Pins live in `~/.local/state/agup/pins.json`, mode 0600.

## Application menu

Installing a bundle also writes a `.desktop` entry and copies the bundle's icon
into the icon theme, so it appears in your application menu -- Whisker, GNOME
Activities, KDE Kickoff -- rather than only working from a shell. User-scope
installs write to `~/.local/share/applications`, so no root is needed.

The icon is placed in the theme directory matching its **actual pixel
dimensions**, read from the PNG header. This matters: the icon theme spec
matches on directory name, so a 1024x1024 icon copied into `512x512/apps`
never resolves and the entry renders with no icon at all. Non-square or
non-standard sizes fall back to an absolute path in `Icon=`, which always
works.

`update-desktop-database` and `gtk-update-icon-cache` are run afterwards, and
XFCE's own menu cache is cleared, since Whisker keeps a cache the desktop
database does not touch. A panel restart is occasionally still needed:

```bash
xfce4-panel -r
```

`--no-desktop` skips it.

## Profile migration

A major release can change `dataFolderName` in product.json. When it does, the
new build starts against an empty profile: settings, extensions and chat
history all appear to be gone, while the old data sits untouched under its old
name. Antigravity did exactly this between 1.23.2 (`.antigravity`,
`~/.config/Antigravity`) and 2.5.5 (`.antigravity-ide`,
`~/.config/Antigravity IDE`).

`agup` detects a renamed profile after installing and says so:

```
  note: 2.5.5 uses a new profile directory (.antigravity-ide).
        Your previous data is still at /home/you/.config/Antigravity (79 MB)
        Nothing was copied. Re-run with --migrate-profile to carry it across.
```

```bash
agup --ide --migrate-profile
```

That copies settings, chat history and workspace state, backs up the new
profile first, and leaves the original entirely alone. Caches and Chromium
scratch directories are skipped -- they are large and regenerate on launch.
Extensions live in the data folder rather than the config directory and are
re-downloadable, so they need `--migrate-extensions` as well.

Note that a recursive copy done by hand tends to fail here: extension trees
contain read-only directories, and preserving those modes means creating a
directory you then cannot write into. The migration forces writable
permissions on what it creates.

## Install paths

Defaults are `~/opt` (user scope) and `/opt` (system scope). If your install
lives elsewhere -- a distro package under `/usr/share`, say -- point at it:

```bash
agup --dir-ide /usr/share/antigravity
```

Or in config:

```toml
[paths]
ide = "/usr/share/antigravity"
```

Two guards apply before anything is replaced:

**Product identity.** The bundle's `product.json` is checked to confirm the
directory holds the component being updated, so an IDE tarball is not written
over a Hub install.

**Package ownership.** If `dpkg` claims the target, the update is skipped
rather than performed. Overwriting package-managed files leaves the package
database describing a tree that no longer exists, and the next `apt upgrade`
will fight whatever replaced it. Either remove the package first, or override
with `--adopt-managed-path` if you know what you are taking on.

## Version detection

Not every component states its version in a file. The IDE is a VS Code fork
with a `product.json`; the Hub is a plain Electron app with neither
`package.json` nor `product.json`, and its `app-update.yml` holds only updater
config. For those, `agup` writes a small `.agup-install.json` receipt into the
install directory recording what it put there. Without it the Hub reads as
"not installed" on every run and gets re-downloaded indefinitely.

The receipt is consulted **last**, after any metadata the bundle itself
carries, so an application that self-updates is never misreported from a stale
file we wrote.


For the IDE specifically: it is a VS Code fork, so `package.json` and `product.json` both carry
a `version` field holding the *upstream Code* version. A build reporting
1.107.0 there is Antigravity 1.23.2. Only `ideVersion` in `product.json` uses
the numbering the release feed speaks, so it is consulted first -- reading
`version` instead compares against an entirely different lineage and every
update decision comes out wrong.

## Notes

Tarballs are extracted with tarfile's `data` filter, which rejects absolute
paths, parent traversal, symlinks escaping the destination, and device nodes.

Installs are swap-and-rollback: the existing directory is moved aside and only
removed once the replacement is in place, so a failure mid-install leaves the
previous version intact.

Process detection ignores the tool's own process tree and matches on the
executable path rather than anywhere in another process's arguments. Matching
loosely on the full command line means a shell that merely *mentions* the
application name gets counted as the running application.

## Tests

```bash
python3 tests/test_core.py    # units
python3 tests/test_e2e.py     # full pipeline against a mock release server
```

The end-to-end suite stands up a local HTTP server, serves synthetic bundles,
and exercises install, upgrade, dry run, checksum mismatch, pin mismatch,
strict mode, unreachable endpoints, and the skip paths.

## Licence

MIT.
