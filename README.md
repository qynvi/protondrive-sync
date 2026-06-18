# ProtonDrive Sync

Textual TUI and background daemon for syncing selected work folders to Proton Drive using the official Proton Drive CLI.

The app is designed for "work relevant" trees, including folders with multiple git repos. It filters out common dependency/build/cache/metadata artifacts and keeps local-file safety as the highest priority.

## Features

- **CLI-backed bidirectional sync**: Uses the official `proton-drive` CLI for upload, download, list, trash, and metadata operations.
- **Targeted sync engine**: Scans local changes, polls the app journal, and syncs only paths that changed instead of running full-tree sync every cycle.
- **Metadata verification**: Verifies transfers with Proton's plaintext `claimedSize`, `claimedDigests.sha1`, and `claimedModificationTime` metadata.
- **Safe remote deletes**: Remote removals go to Proton Drive trash, not permanent delete.
- **Local overwrite protection**: Local paths are backed up into `.protondrive-sync-backups/` before destructive local changes.
- **Initial setup safety**: Empty-local downloads are staged and verified before publishing; uploads skip already-matching files on retry and fail on mismatches.
- **Git metadata support**: Scans synced folders for git metadata and supports rehydrating repos on new machines.
- **Symlink modes**: Preserve symlinks as `.rclonelink` metadata blobs, copy targets, or skip links.
- **Filters**: Excludes `.git/`, `node_modules/`, `__pycache__/`, build artifacts, caches, and other noisy data by default.
- **Background daemon**: User-level systemd service on Linux or Task Scheduler on Windows.

Mount/FUSE mode and rclone are no longer used.

## Prerequisites

- Python 3.11+
- Proton Drive CLI authentication: `vendor/proton-drive-cli/proton-drive auth login`

The installer vendors the pinned Proton Drive CLI binary into `vendor/proton-drive-cli/proton-drive` using `scripts/install-proton-cli.sh`.

## Installation

```bash
./install.sh
```

This creates `.venv/`, installs the package, downloads/verifies the Proton Drive CLI if needed, installs the icon, and creates the desktop launcher.

If you already downloaded the CLI manually, you can install it into the app vendor directory with checksum verification:

```bash
scripts/install-proton-cli.sh --from ~/Desktop/proton-drive
```

## Usage

Launch via desktop shortcut, `./launch.sh`, or `.venv/bin/protondrive-sync`.

First-time flow:

1. Authenticate once: `vendor/proton-drive-cli/proton-drive auth login`
2. Launch the TUI.
3. Press `a` to add a folder mapping.
4. Press `d` to install/start the background daemon.

Keyboard shortcuts:

| Key | Action |
|-----|--------|
| `a` | Add folder |
| `e` | Edit folder |
| `r` | Remove folder |
| `g` | Git rehydration |
| `v` | Review flagged changes |
| `y` | Manual verify |
| `l` | Live daemon logs |
| `d` | Service control |
| `s` | Settings |
| `f` | Refresh |
| `q` | Quit |

## Adding A Folder

Provide a local path and a remote subpath under Proton Drive `/my-files`.

Examples:

- Local `/home/user/work/project-a`
- Remote subpath `workspace/project-a`
- CLI path used internally: `/my-files/workspace/project-a`

Setup cases:

- Local has data, remote missing/empty: upload local files, verify metadata, seed baseline.
- Local empty/missing, remote has data: download into a sibling temp directory, verify metadata, then publish local folder.
- Both have unrelated data: blocked for review rather than merged automatically.

## Configuration

Config lives at `~/.config/protondrive-sync/config.json`.

Common settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `proton_cli_path` | `null` | Optional explicit CLI binary path override |
| `proton_cli_concurrency` | `4` | Parallel CLI invocations for remote walks |
| `symlink_mode` | `preserve` | `preserve`, `copy`, or `skip` |
| `bisync_check_interval` | `15` | Seconds between local modification scans |
| `bisync_quiet_threshold` | `120` | Quiet seconds before a sync fires |
| `bisync_max_burst` | `1800` | Max coalescing window |
| `filters` | built-in defaults | App-side include/exclude rules |

## Architecture

Key modules:

- `core/proton_cli.py`: subprocess wrapper for the Proton Drive CLI; path mapping, JSON parsing, trash, upload/download, metadata extraction.
- `core/sync_engine.py`: targeted bidirectional sync planner/executor and inventory/journal updates.
- `core/migration.py`: initial setup and metadata baseline seeding.
- `core/inventory.py`: SQLite inventory of last verified local/remote state.
- `core/journal.py`: app-owned remote journal for multi-machine coordination.
- `core/verify.py`: metadata-based subtree verification.
- `bisync_main.py`: adaptive daemon loop.
- `service/systemd.py`: Linux user-service generation.

## Troubleshooting

- **CLI not found**: Run `scripts/install-proton-cli.sh`.
- **Not authenticated**: Run `vendor/proton-drive-cli/proton-drive auth login` from your user session.
- **Daemon auth issue**: The CLI was validated under `systemd --user`; restart the user service after login if credentials expire.
- **Unexpected sync block**: Check pending review (`v`) or daemon logs (`l`). The app blocks uncertain local/remote conflicts instead of overwriting.
- **Need remote recovery**: Remote deletes/replaces are recoverable from Proton Drive trash/version history.

## License

MIT
