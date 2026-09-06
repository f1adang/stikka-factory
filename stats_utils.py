"""Statistics tracking utilities for print jobs.

Deliberately pure stdlib. Streamlit pulls in pandas/numpy/pyarrow anyway, but
nothing here touches them: the stats feature was disabled once already because
native code crashed the app on the Raspberry Pi hosts, and a SIGILL cannot be
caught, so the only defence is not to call into that code at all.
"""

import json
import logging
import os
import threading
from datetime import datetime
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("sticker_factory.stats_utils")

# Anchored to the project directory rather than the cwd, so stats land in the
# same place no matter where the app is launched from (systemd units on the
# print hosts don't necessarily start in the repo root).
STATS_FILE = Path(__file__).parent / "print_stats.json"

# Keep the file bounded; a booth can print a lot over a weekend.
MAX_RECORDS = 10000

# record_print() runs on the print-queue worker thread while the Streamlit
# threads read. Guards the read-modify-write so concurrent prints can't drop
# each other's records.
_stats_lock = threading.Lock()


def load_stats():
    """Load statistics. Returns a list of records, empty if there are none."""
    if not os.path.exists(STATS_FILE):
        return []

    try:
        with open(STATS_FILE, "r") as f:
            stats = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # Don't return [] and let the next save overwrite a file we simply
        # failed to parse - that would silently destroy the history. Move it
        # aside so it can be recovered by hand.
        logger.error(f"Could not read {STATS_FILE}: {e}")
        _quarantine_corrupt_file()
        return []

    if not isinstance(stats, list):
        logger.error(f"{STATS_FILE} does not contain a list, ignoring it")
        _quarantine_corrupt_file()
        return []

    return stats


def _quarantine_corrupt_file():
    """Rename an unreadable stats file instead of letting it be overwritten."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        # Second resolution isn't unique enough - two bad reads in the same
        # second would have the later backup clobber the earlier one, losing
        # exactly the data this is meant to preserve. Probe for a free name.
        backup = STATS_FILE.with_suffix(f".corrupt-{stamp}.json")
        n = 1
        while backup.exists():
            backup = STATS_FILE.with_suffix(f".corrupt-{stamp}-{n}.json")
            n += 1
        os.replace(STATS_FILE, backup)
        logger.warning(f"Moved unreadable stats file to {backup}")
    except OSError as e:
        logger.error(f"Could not set aside the corrupt stats file: {e}")


def save_stats(stats):
    """Write statistics atomically. Returns True on success."""
    tmp = STATS_FILE.with_suffix(".tmp")
    try:
        # Write to a temp file and rename: os.replace is atomic, so pulling the
        # power mid-write leaves the previous file intact rather than a
        # half-written one that parses as nothing.
        with open(tmp, "w") as f:
            json.dump(stats, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATS_FILE)
        return True
    except OSError as e:
        logger.error(f"Error saving stats: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def record_print(printer_name, printer_model=None):
    """Record a successful print job."""
    record = {
        "timestamp": datetime.now().isoformat(),
        "printer_name": printer_name,
        "printer_model": printer_model or "",
    }

    with _stats_lock:
        stats = load_stats()
        stats.append(record)
        if len(stats) > MAX_RECORDS:
            stats = stats[-MAX_RECORDS:]
        save_stats(stats)

    logger.debug(f"Recorded print for printer: {printer_name}")


def _record_time(record):
    """Parse a record's timestamp, or None if it's unusable."""
    try:
        return datetime.fromisoformat(record["timestamp"])
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"Skipping record with bad timestamp: {e}")
        return None


def get_dashboard_stats():
    """Everything the stats tab needs, from a single read of the file.

    Rendering used to call four separate helpers, each re-reading and
    re-parsing the whole file. Returns a dict with:
        total_prints, printers {name: count}, by_date {date: {name: count}},
        first_print, last_print, today
    """
    stats = load_stats()

    totals = defaultdict(int)
    by_date = defaultdict(lambda: defaultdict(int))
    times = []
    today = datetime.now().date()
    prints_today = 0

    for record in stats:
        printer = record.get("printer_name") or "Unknown"
        totals[printer] += 1

        when = _record_time(record)
        if when is None:
            continue
        by_date[when.date().isoformat()][printer] += 1
        times.append(when)
        if when.date() == today:
            prints_today += 1

    return {
        "total_prints": len(stats),
        "printers": dict(totals),
        "by_date": {d: dict(p) for d, p in by_date.items()},
        # Full timestamps, not dates: the tab renders "last print N minutes ago".
        "first_print": min(times).isoformat() if times else None,
        "last_print": max(times).isoformat() if times else None,
        "today": prints_today,
    }


def get_stats_by_date(printer_name=None):
    """Statistics grouped by date and printer: {date: {printer_name: count}}."""
    by_date = get_dashboard_stats()["by_date"]
    if printer_name is None:
        return by_date
    return {
        date: {p: c for p, c in printers.items() if p == printer_name}
        for date, printers in by_date.items()
        if printer_name in printers
    }


def get_total_stats():
    """Total prints per printer."""
    return get_dashboard_stats()["printers"]


def get_stats_summary():
    """Summary statistics."""
    data = get_dashboard_stats()
    return {
        "total_prints": data["total_prints"],
        "printers": data["printers"],
        "first_print": data["first_print"],
        "last_print": data["last_print"],
    }


def get_prints_today():
    """Count of prints made today (resets at midnight)."""
    return get_dashboard_stats()["today"]


def get_prints_total():
    """Total count of all prints."""
    return get_dashboard_stats()["total_prints"]
