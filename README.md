# ProtonDrive Sync

TUI management tool for syncing local folders to Proton Drive via rclone. Supports bidirectional sync (bisync) and FUSE mount modes with adaptive timing, safety checks, and git repo rehydration.

## Features

- **Bisync** (default): Bidirectional sync via `rclone bisync` — local directory stays as-is, no symlinks
- **Mount mode** (alternative): FUSE mount with VFS caching, symlink from local path to mount point
- **Adaptive sync timing**: Activity-window coalescing — 15s check interval, 120s quiet threshold, 30min max burst
- **Delete protection**: Deleted work files backed up to `.protondrive-sync-backups/` on remote with timestamp suffix
- **Suspicious change detection**: Blocks sync when >50% size change on >10KB files until user approves in TUI
- **Git rehydration**: Scans synced folders for git metadata, reconnects repos on new devices (clone, fetch, branch tracking, submodules)
- **Low-footprint mode**: Single toggle — limits CPU (transfers=1, checkers=1, nice=19) and bandwidth (2M up / 10M down)
- **Follow symlinks**: `--copy-links` on by default, configurable in Settings
- **Filter rules**: `.git/`, `node_modules/`, `__pycache__/`, etc. excluded from sync
- **Instance locking**: Only one TUI window at a time (flock-based, auto-releases on crash)
- **Cross-platform**: Python — works on aarch64 and x86_64, Linux and Windows

## Prerequisites

- **Python 3.11+**
- **rclone v1.62+** (Proton Drive backend added in 1.62; Ubuntu apt is too old — install from rclone.org)
- **FUSE** (only for mount mode): `fuse3` on Linux, WinFsp on Windows

## Installation

### 1. Install rclone

```bash
# ARM64
curl -L -O https://downloads.rclone.org/current/rclone-current-linux-arm64.deb
sudo dpkg -i rclone-current-linux-arm64.deb

# x86_64
curl -L -O https://downloads.rclone.org/current/rclone-current-linux-amd64.deb
sudo dpkg -i rclone-current-linux-amd64.deb

# Any platform
curl https://rclone.org/install.sh | sudo bash
```

### 2. Configure rclone

```bash
rclone config
# Create remote: type 'protondrive', name it 'proton'
# Verify: rclone lsd proton:
```

**Password with special characters:** Use `rclone obscure 'YourP@ss!'` and edit `~/.config/rclone/rclone.conf` directly.

**2FA:** Enter the base32 TOTP seed (e.g. `ABCDEFGHIJKLMNOP`), not a 6-digit code.

### 3. Install the app

```bash
./install.sh
# Creates venv, installs deps, icon, and .desktop launcher
```

## Usage

Launch via desktop shortcut or `./launch.sh` or `.venv/bin/protondrive-sync`.

### First-time setup

1. `s` (Settings) — set remote name to match your rclone config
2. `a` (Add folder) — map a local directory to Proton Drive
3. `d` (Service) — install and start the background sync daemon

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `a` | Add folder |
| `e` | Edit folder |
| `r` | Remove folder |
| `g` | Git rehydration |
| `v` | Review flagged changes |
| `l` | Live daemon logs |
| `p` | Pin settings (mount mode) |
| `d` | Service control |
| `s` | Settings |
| `f` | Refresh |
| `q` | Quit |

### Adding a folder

Provide a local path and remote subpath. The local directory name is auto-appended:

`/home/user/notes` + remote `workspace` → `proton:workspace/notes`

Path inputs have ghost-text autocomplete. Press Browse for modal directory browsers.

## Configuration

Stored at `~/.config/protondrive-sync/config.json`.

| Setting | Default | Description |
|---------|---------|-------------|
| `remote_name` | `protondrive` | rclone remote name |
| `mount_point` | `~/ProtonDrive` | FUSE mount location (mount mode) |
| `transfers` | `8` | Concurrent transfer streams |
| `checkers` | `16` | Concurrent integrity checkers |
| `copy_links` | `true` | Follow symlinks (`--copy-links`) |
| `low_footprint` | `false` | Limit CPU + bandwidth |
| `bisync_check_interval` | `15` | Seconds between modification scans |
| `bisync_quiet_threshold` | `120` | Seconds of quiet before sync fires |
| `bisync_max_burst` | `1800` | Max seconds to coalesce before forced sync |
| `size_change_threshold` | `0.5` | Flag files with >50% size change |
| `size_change_min_bytes` | `10240` | Only flag files >10KB |
| `filters` | see below | rclone filter rules |

## Architecture

```
protondrive-sync/
├── src/protondrive_sync/
│   ├── app.py                # Textual App, instance locking, CSS
│   ├── bisync_main.py        # Adaptive sync daemon (systemd)
│   ├── pinner_main.py        # Cache pinner daemon (mount mode)
│   ├── core/
│   │   ├── config.py         # AppConfig, FolderMapping, JSON persistence
│   │   ├── platform.py       # OS detection, XDG paths, instance lock
│   │   ├── rclone.py         # rclone subprocess wrapper
│   │   ├── bisync.py         # Delete protection, change detection, coalescing
│   │   ├── migration.py      # Upload, verify, bisync init, mount migration
│   │   ├── git_meta.py       # Git scanning, metadata, rehydration
│   │   ├── suggesters.py     # Path autocomplete for Input widgets
│   │   ├── symlinks.py       # Symlink/junction management
│   │   └── pinner.py         # Background cache pinning
│   ├── screens/
│   │   ├── main.py           # Folder table, status dashboard
│   │   ├── add_folder.py     # Add/edit folder with migration
│   │   ├── settings.py       # Global settings
│   │   ├── service_control.py
│   │   ├── logs.py           # Live daemon log viewer
│   │   ├── rehydrate.py      # Git rehydration
│   │   ├── review.py         # Flagged changes review
│   │   ├── path_browser.py   # Local/remote directory browser
│   │   ├── pin_settings.py   # Per-folder cache pinning
│   │   └── confirm.py        # Confirmation dialog
│   └── service/
│       ├── systemd.py        # systemd unit generation
│       └── windows.py        # Windows Task Scheduler
├── tests/                    # 165 tests
├── resources/
│   └── protondrive-sync.svg
├── install.sh
├── launch.sh                 # Terminal resize wrapper
└── pyproject.toml
```

## Troubleshooting

- **"rclone not found"**: Install from https://rclone.org/install/ (not apt)
- **"Incorrect login credentials"**: Re-run `rclone config`. For special chars in password, use `rclone obscure`
- **"Decoding of secret as base32 failed"**: Enter the TOTP seed, not a 6-digit code
- **"draft already exists" (error 2501)**: App handles this with `--protondrive-replace-existing-draft`
- **Desktop icon missing**: Right-click > Allow Launching. Log out/in to refresh icon cache
- **Another instance running**: TUI shows the holder PID and offers to kill it

## License

MIT
