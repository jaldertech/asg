"""
Dynamic Scrub Controller — heuristic-throttled BTRFS scrub scheduling.

Starts a non-blocking scrub and monitors system load and per-device I/O
utilisation. Pauses the scrub when the system is busy (e.g. during media
streaming or large file moves) and resumes when idle.

All I/O monitoring uses /proc/loadavg and /proc/diskstats directly —
no external dependencies required.
"""

import os
import re
import subprocess
import time
from datetime import datetime

from . import config
from .pool_validator import require_pool, get_pool_mount
from . import notifier


def _log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] scrub: {message}")


def _get_pool_kernel_names() -> list[str]:
    """Auto-detect kernel device names (e.g. 'sdc') for the pool."""
    mount = get_pool_mount()
    try:
        result = subprocess.run(
            ["sudo", "btrfs", "filesystem", "show", mount],
            capture_output=True, text=True, timeout=10,
        )
        names = []
        for line in result.stdout.splitlines():
            match = re.search(r"path\s+(/dev/(\w+))", line)
            if match:
                names.append(match.group(2))
        return names
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


# ── Load monitoring ──────────────────────────────────────────────────

def get_load_average() -> float:
    """Return the 1-minute load average from /proc/loadavg."""
    try:
        with open("/proc/loadavg", "r") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _read_diskstats() -> dict[str, dict[str, int]]:
    """
    Parse /proc/diskstats for pool devices.

    Returns: {"sdc": {"io_ticks": ...}, ...}
    """
    kernel_names = _get_pool_kernel_names()
    stats: dict[str, dict[str, int]] = {}
    try:
        with open("/proc/diskstats", "r") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 14:
                    continue
                name = parts[2]
                if name in kernel_names:
                    fields = [int(x) for x in parts[3:]]
                    stats[name] = {
                        "read_ios": fields[0],
                        "write_ios": fields[4],
                        "io_ticks": fields[9],
                    }
    except (OSError, ValueError, IndexError):
        pass
    return stats


def get_io_utilisation(interval: float = 2.0) -> dict[str, float]:
    """
    Measure I/O utilisation percentage for each pool device over *interval* seconds.

    100% means the device was busy for the entire interval.
    """
    before = _read_diskstats()
    time.sleep(interval)
    after = _read_diskstats()

    interval_ms = interval * 1000.0
    util: dict[str, float] = {}

    for name in before:
        if name in after:
            delta = after[name]["io_ticks"] - before[name]["io_ticks"]
            pct = min((delta / interval_ms) * 100.0, 100.0)
            util[name] = round(pct, 1)

    return util


def get_pool_write_iops(interval: float = 2.0) -> float:
    """
    Aggregate WRITE IOPS across the pool devices over *interval* seconds.

    A BTRFS scrub is sequential-read-only except for rare checksum repair,
    so sustained writes to the pool devices are a reliable signal of genuine
    external load (file writes, moves, backups). This is used instead of
    total device-busy time while a scrub is running, because io_ticks is
    then dominated by the scrub's own reads and cannot distinguish it from
    real contention.
    """
    before = _read_diskstats()
    time.sleep(interval)
    after = _read_diskstats()

    if interval <= 0:
        return 0.0

    total_writes = 0
    for name in before:
        if name in after:
            total_writes += after[name]["write_ios"] - before[name]["write_ios"]

    return round(total_writes / interval, 1)


def is_system_busy(running: bool = False) -> tuple[bool, str]:
    """
    Determine whether the system is too busy for scrubbing.

    Returns (is_busy, reason_string).

    When *running* is False (pre-flight, before a scrub is started or
    resumed) the scrub has no footprint yet, so absolute load average and
    per-device I/O utilisation are valid signals of existing activity.

    When *running* is True (a scrub is already in progress) those signals
    are dominated by the scrub itself — its per-device checksum-verification
    kworkers each add roughly 1.0 to load average, and its sequential reads
    keep the pool devices busy — so an elevated load threshold and pool
    WRITE IOPS are used instead. Applying the pre-flight thresholds mid-run
    makes the controller cancel the scrub in response to its own load,
    producing a cancel/resume thrash loop.
    """
    scrub_cfg = config.get()["scrub"]

    load_threshold = (
        scrub_cfg["load_threshold_running"] if running
        else scrub_cfg["load_threshold"]
    )
    load = get_load_average()
    if load > load_threshold:
        return True, f"load average {load:.2f} exceeds threshold {load_threshold}"

    if running:
        write_threshold = scrub_cfg["write_iops_threshold"]
        write_iops = get_pool_write_iops(interval=2.0)
        if write_iops > write_threshold:
            return True, (
                f"pool write load {write_iops:.0f} IOPS exceeds threshold "
                f"{write_threshold:.0f} IOPS"
            )
        return False, ""

    io_threshold = scrub_cfg["io_threshold_percent"]
    io_util = get_io_utilisation(interval=2.0)
    for device, pct in io_util.items():
        if pct > io_threshold:
            return True, f"{device} I/O utilisation {pct}% exceeds threshold {io_threshold}%"

    return False, ""


# ── Scrub commands ───────────────────────────────────────────────────

def _scrub_start(dry_run: bool = False) -> bool:
    mount = get_pool_mount()
    cmd = ["sudo", "btrfs", "scrub", "start", mount]
    if dry_run:
        _log(f"DRY RUN: would execute: {' '.join(cmd)}")
        return True
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            _log("Scrub started successfully.")
            return True
        else:
            _log(f"Scrub start failed: {result.stderr.strip()}")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        _log(f"Scrub start error: {exc}")
        return False


def _scrub_cancel(dry_run: bool = False) -> bool:
    mount = get_pool_mount()
    cmd = ["sudo", "btrfs", "scrub", "cancel", mount]
    if dry_run:
        _log(f"DRY RUN: would execute: {' '.join(cmd)}")
        return True
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            _log("Scrub cancelled (paused).")
            return True
        else:
            _log(f"Scrub cancel failed: {result.stderr.strip()}")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        _log(f"Scrub cancel error: {exc}")
        return False


def _scrub_status() -> dict:
    mount = get_pool_mount()
    try:
        result = subprocess.run(
            ["sudo", "btrfs", "scrub", "status", mount],
            capture_output=True, text=True, timeout=15,
        )
        output = result.stdout
        running = "running" in output.lower() and "finished" not in output.lower()

        errors = 0
        for line in output.splitlines():
            if "error summary" in line.lower():
                if "no errors" in line.lower():
                    errors = 0
                else:
                    nums = re.findall(r"(\d+)", line)
                    if nums:
                        errors = sum(int(n) for n in nums)

        return {"running": running, "raw": output.strip(), "errors": errors}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"running": False, "raw": "status unavailable", "errors": -1}


# ── Main controller loop ────────────────────────────────────────────

def run_scrub(dry_run: bool = False) -> None:
    """Execute a heuristic-throttled scrub of the pool."""
    require_pool()
    cfg = config.get()
    mount = get_pool_mount()
    poll_interval = cfg["scrub"]["poll_interval_seconds"]
    grace_period = cfg["scrub"]["grace_period_seconds"]

    start_time = datetime.now()
    _log(f"Scrub controller initiated for {mount}")
    pause_count = 0
    total_pause_secs = 0

    # Pre-flight: wait for idle
    busy, reason = is_system_busy()
    if busy:
        _log(f"System is currently busy ({reason}). Waiting for idle window...")

    while True:
        busy, reason = is_system_busy()
        if not busy:
            break
        _log(f"Deferring scrub start: {reason}")
        if dry_run:
            _log("DRY RUN: would wait for idle window, skipping.")
            break
        time.sleep(poll_interval)

    if not _scrub_start(dry_run=dry_run):
        _log("Aborting: scrub could not be started.")
        return

    if dry_run:
        _log("DRY RUN: scrub cycle simulation complete.")
        return

    _log(f"Grace period: {grace_period}s before throttle checks begin.")
    time.sleep(grace_period)

    scrub_paused = False
    pause_start = 0.0

    while True:
        status = _scrub_status()

        if not status["running"] and not scrub_paused:
            _log("Scrub has finished.")
            break

        # running=True: a scrub is in progress, so judge contention by the
        # elevated load threshold and pool write IOPS rather than signals
        # the scrub's own sequential reads dominate.
        busy, reason = is_system_busy(running=True)

        if busy and not scrub_paused:
            _log(f"Throttling: pausing scrub — {reason}")
            _scrub_cancel()
            scrub_paused = True
            pause_start = time.monotonic()
            pause_count += 1

        elif not busy and scrub_paused:
            pause_duration = time.monotonic() - pause_start
            total_pause_secs += pause_duration
            _log(f"System idle. Resuming scrub (was paused for {pause_duration:.0f}s).")
            if not _scrub_start():
                _log("Failed to resume scrub. Will retry next cycle.")
                time.sleep(poll_interval)
                continue
            scrub_paused = False
            time.sleep(grace_period)
            continue

        time.sleep(poll_interval)

    # Final report
    end_time = datetime.now()
    elapsed = end_time - start_time
    final_status = _scrub_status()

    _log(f"Scrub complete. {pause_count} pause(s), {final_status.get('errors', '?')} error(s).")

    scrub_color = 0x2ECC71 if final_status.get("errors", 0) == 0 else 0xE74C3C
    notifier.send(
        "Scrub Completed",
        f"**Duration:** {elapsed}\n"
        f"**Pauses:** {pause_count} ({total_pause_secs:.0f}s total)\n"
        f"**Errors:** {final_status.get('errors', 'unknown')}",
        color=scrub_color,
    )

    if final_status.get("errors", 0) > 0:
        notifier.send(
            "CRITICAL: Scrub Found Errors",
            f"Scrub of `{mount}` completed with {final_status['errors']} error(s).\n"
            f"Immediate investigation required.\n```\n{final_status['raw']}\n```",
            color=0xE74C3C,
        )
