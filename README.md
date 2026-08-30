<p align="center">
  <img src="https://raw.githubusercontent.com/jaldertech/asg/main/assets/logo.png" alt="Aldertech Logo" width="200"/>
</p>

# ASG (Aldertech Storage Governor)

> **"Why does `df -h` say I have 2.6TB free but BTRFS thinks it's 3.5TB?"**
>
> Because `df` is wrong — and nobody else fixes this.

ASG (Aldertech Storage Governor) is a lightweight, zero-dependency Python tool that provides accurate health monitoring, capacity planning, and intelligent scrub scheduling for BTRFS RAID1 pools — especially those with **mismatched drive sizes**.

If you've ever mixed a 4TB drive with two 1TB drives and wondered why your free space numbers don't add up, ASG is for you.

---

## The Problem

BTRFS RAID1 with mismatched drives is powerful but poorly served by standard tools:

- **`df -h` is mathematically wrong.** It doesn't account for RAID1 pairing constraints on mismatched drives.
- **`btrfs scrub` is fire-and-forget.** The standard approach (`btrfs scrub start -Bd`) blocks indefinitely with zero awareness of system load — your media server stutters while the scrub runs.
- **Metadata concentration is invisible.** BTRFS may silently place all metadata chunks on your two smallest drives. If either fails, the entire pool is unrecoverable. No standard tool warns you about this.
- **No capacity prediction.** Without historical tracking, you won't know your pool is filling up until it's too late.

## The Solution

ASG provides five integrated governance modules:

1. **Real Capacity Engine** — Calculates true RAID1 free space using the formula `min(sum/2, sum - largest)`, tracks historical usage, and predicts days-to-full.
2. **Heuristic Scrub Controller** — Runs scrubs in the background, monitoring `/proc/loadavg` and `/proc/diskstats`. Automatically pauses when your system is busy and resumes when idle.
3. **Integrity Monitor** — Polls `btrfs device stats` for all drives, detects errors, and sends alerts with deduplication.
4. **Metadata Concentration Watchdog** — Warns if metadata/system chunks are dangerously concentrated on a subset of drives.
5. **Snapshot Bridge** — Creates atomic read-only snapshots before your backup software runs, with automatic cleanup.

---

## Features

- **Zero dependencies** — Python standard library only (PyYAML optional for config)
- **Accurate RAID1 maths** for 2, 3, 4, or more mismatched drives
- **Load-aware scrub throttling** using `/proc/loadavg` and `/proc/diskstats`
- **Metadata concentration alerts** — catches the silent killer
- **Days-to-full prediction** from historical usage data
- **Pre-backup atomic snapshots** with retention management
- **Pool UUID validation** before every operation (prevents wrong-pool disasters)
- **`--dry-run`** on all filesystem-modifying commands
- **Notifications** — Discord, NTFY, and Gotify (all optional)
- **Config-driven** — single YAML file, sensible defaults

---

## Requirements

| Requirement | Minimum | Notes |
|---|---|---|
| Linux kernel | 4.0+ | Any kernel with BTRFS support |
| btrfs-progs | 5.0+ | `sudo apt install btrfs-progs` |
| Python | 3.9+ | |
| BTRFS profile | RAID1 | RAID1C3/RAID1C4 should also work |
| sudo | Required | BTRFS commands require root |
| PyYAML | Optional | For config file; falls back to defaults without it |

---

## Quick Start

### Option 1: Install via pip

```bash
pip3 install aldertech-asg
```

### Option 2: Install via Git Clone

```bash
# 1. Clone the repository
git clone https://github.com/jaldertech/asg.git
cd asg

# 2. Run the installer (requires root)
sudo bash setup.sh

# 3. Edit your config
sudo nano /etc/asg/config.yaml

# 4. Test it
asg status
```

### Option 3: Run directly (no install)

```bash
git clone https://github.com/jaldertech/asg.git
cd asg
python3 -m asg status
```

---

## Configuration

Copy `config.yaml` to `/etc/asg/config.yaml` and edit it:

```yaml
pool:
  mount: "/mnt/my_pool"
  # uuid: "optional-pin-for-safety"

scrub:
  load_threshold: 3.0           # Pre-flight: don't start/resume above this 1-min load average
  io_threshold_percent: 40.0    # Pre-flight: don't start/resume if any drive exceeds this I/O %
  load_threshold_running: 7.0   # Running: pause an in-progress scrub above this load average
  write_iops_threshold: 600.0   # Running: pause an in-progress scrub above this pool write IOPS

capacity:
  chunk_fullness_warn_percent: 90.0
  free_space_warn_percent: 10.0
  metadata_min_device_count: 3  # Warn if metadata on fewer devices

snapshots:
  directory: ".snapshots"       # Relative to pool mount
  retention_days: 3

# Optional — remove if you don't want notifications
notifications:
  discord:
    webhook_url: "https://discord.com/api/webhooks/..."
```

If PyYAML is not installed, the tool runs with built-in defaults (mount point: `/mnt/media_pool`).

---

## Usage

```bash
# Full status overview (integrity + capacity + snapshots)
asg status

# Real capacity report with days-to-full prediction
asg capacity

# Run a load-aware scrub (pauses during high I/O)
asg scrub --dry-run     # Preview first
asg scrub               # Run for real

# Check all drives for errors
asg check

# Pre-backup snapshot (cleanup old + create new)
asg snapshot --dry-run
asg snapshot

# Clean up expired snapshots only
asg cleanup

# Use a custom config path
asg --config /path/to/config.yaml status
```

### Example Output: Capacity Report

```
ASG Capacity Report — /mnt/media_pool
Timestamp: 2026-03-07 19:45:57
=================================================================

  Total Raw Capacity:   7454.1 GiB
  Largest Drive:        3727.4 GiB
  RAID1 Usable Ceiling: 3726.7 GiB
  Data Used:            100.5 GiB
  Real Free Space:      3625.8 GiB
  Utilisation:          2.7%
  Days to Full:         ~342

  Per-Device Allocation:
  Device           Data       Meta     System      Unalloc      Total
  ---------------------------------------------------------------
  /dev/sdc         103.0G     1.000G     0.031G      1761.3G    1863.7G
  /dev/sdd          30.0G     1.000G     0.008G       900.5G     931.5G
  /dev/sde          25.0G     1.000G     0.008G       905.5G     931.5G
  /dev/sdf          50.0G     1.000G     0.031G      3676.2G    3727.4G

  WARNINGS:
    ! Data chunks are 96.6% full — new chunk allocation imminent.
=================================================================
```

---

## Cron Integration

Add to your crontab (`crontab -e`):

```bash
# Daily capacity report at 00:20
20 0 * * * asg capacity >> ~/asg.log 2>&1

# Weekly throttled scrub on Sunday at 02:00
0 2 * * 0 asg scrub >> ~/asg.log 2>&1

# Daily pre-backup snapshot at 02:00
0 2 * * * asg snapshot >> ~/asg.log 2>&1
```

---

## How the RAID1 Maths Works

Standard `df` divides total raw space by the data ratio (2.0 for RAID1), giving `sum / 2`. But this ignores the **pairing constraint**: each RAID1 chunk must fit on two different drives.

For mismatched drives, the real limit is:

```
usable = min(sum_of_all_drives / 2, sum_of_all_drives - largest_drive)
```

**Example:** 4TB + 2TB + 1TB + 1TB = 8TB raw

| Calculation | Result | Why |
|---|---|---|
| `sum / 2` | 4 TB | Standard RAID1 formula |
| `sum - largest` | 4 TB | The 4TB drive can only mirror 4TB of data across the other 3 drives |
| **Usable ceiling** | **4 TB** | `min(4, 4)` — balanced in this case |

But with 8TB + 1TB + 1TB = 10TB raw:

| Calculation | Result | Why |
|---|---|---|
| `sum / 2` | 5 TB | Looks generous |
| `sum - largest` | 2 TB | The 8TB drive can only mirror 2TB across the others |
| **Usable ceiling** | **2 TB** | 6TB of the 8TB drive is **wasted** |

This tool calculates the correct number automatically.

---

## Verified Hardware

| Hardware | OS | Pool Layout | Status |
|---|---|---|---|
| Raspberry Pi 5 (16GB) | Raspberry Pi OS Bookworm | 4TB + 2TB + 1TB + 1TB RAID1 | Verified |

ASG uses generic Linux interfaces (`/proc`, `btrfs-progs`). It should work on any Linux system with BTRFS. If you test on other hardware, please open a PR to add it to this table.

---

## Notifications

All backends are optional and independent. Configure any combination in `config.yaml`:

| Backend | Config Key | Notes |
|---|---|---|
| Discord | `notifications.discord.webhook_url` | Standard Discord webhook |
| NTFY | `notifications.ntfy.url` | Full topic URL |
| Gotify | `notifications.gotify.url` + `.token` | Base URL + app token |

---

## Security

ASG runs `btrfs` commands via `sudo`. It requires that the executing user has passwordless `sudo` for `btrfs` (standard on Raspberry Pi OS) or is run as root.

The tool:
- Never writes to arbitrary paths (only state files in a configured directory)
- Validates the pool UUID before every operation
- Supports `--dry-run` for all destructive commands
- Has zero network dependencies (notifications are opt-in)

---

## Related Projects

- **[ADRG](https://github.com/jaldertech/adrg)** — Aldertech Dynamic Resource Governor: Automatic CPU/RAM management for high-density Docker stacks.

## Licence

MIT — see [LICENCE](LICENCE) file.

---

*Built by [Aldertech](https://aldertech.uk)*
