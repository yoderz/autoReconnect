#!/usr/bin/env python3
"""
Auto Reconnect Script
Pings www.google.com and reconnects WiFi if 4 consecutive failures occur.
"""

import subprocess
import time
import sys
import re
import csv
import os
from collections import deque
from datetime import datetime, timedelta


# In-memory per-hour statistics
# Key: datetime truncated to hour (YYYY-mm-dd HH:00)
# Value: {"latencies": [ms, ...], "resets": int}
hourly_stats = {}

# Reset/SSID tracking (based on initial SSID, resets, and periodic checks)
reset_events = []  # list of (datetime, ssid)
script_start_time = None
current_ssid = "unknown"

# Hourly CSV summary log
HOURLY_CSV_LOG_PATH = "auto_reconnect_hourly.csv"

# Persistent SSID usage log (across runs)
SSID_USAGE_CSV_PATH = "ssid_usage.csv"


def load_ssid_usage_csv(path=SSID_USAGE_CSV_PATH):
    """
    Load SSID usage from CSV.
    Format: SSID,Hours used,Total Resets
    Returns dict: {ssid: {"hours_used": float, "total_resets": int}}
    """
    data = {}
    if not os.path.exists(path):
        return data

    try:
        with open(path, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                if row[0].strip().lower() == "ssid":
                    continue
                if len(row) < 3:
                    continue
                ssid = row[0].strip()
                if not ssid:
                    continue
                try:
                    hours_used = float(row[1])
                except Exception:
                    hours_used = 0.0
                try:
                    total_resets = int(float(row[2]))
                except Exception:
                    total_resets = 0

                data[ssid] = {"hours_used": hours_used, "total_resets": total_resets}
    except Exception as e:
        print(f"Error reading SSID usage CSV: {e}")

    return data


def write_ssid_usage_csv(data, path=SSID_USAGE_CSV_PATH):
    """Write SSID usage CSV sorted by hours_used descending."""
    try:
        rows = sorted(
            ((ssid, v.get("hours_used", 0.0), v.get("total_resets", 0)) for ssid, v in data.items()),
            key=lambda r: (r[1], r[2], r[0]),
            reverse=True,
        )
        with open(path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["SSID", "Hours used", "Total Resets"])
            for ssid, hours_used, total_resets in rows:
                writer.writerow([ssid, f"{hours_used:.4f}", int(total_resets)])
    except Exception as e:
        print(f"Error writing SSID usage CSV: {e}")


def _ensure_hourly_csv_header():
    if os.path.exists(HOURLY_CSV_LOG_PATH):
        return
    try:
        with open(HOURLY_CSV_LOG_PATH, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "date",
                    "hour",
                    "ping_count",
                    "latency_min_ms",
                    "latency_q1_ms",
                    "latency_median_ms",
                    "latency_q3_ms",
                    "latency_max_ms",
                    "wifi_resets",
                    "ssid",
                    "ssid_minutes",
                ]
            )
    except Exception as e:
        print(f"Error initializing hourly CSV log: {e}")


def log_hourly_to_csv(hour, stats, summary, ssid_minutes):
    """Append one row per SSID for this hour to the hourly CSV log."""
    try:
        _ensure_hourly_csv_header()
        date_str = hour.strftime("%Y-%m-%d")
        hour_label = hour.strftime("%H:00")
        ping_count = len(stats["latencies"])

        with open(HOURLY_CSV_LOG_PATH, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if ssid_minutes:
                for ssid, minutes in ssid_minutes.items():
                    writer.writerow(
                        [
                            date_str,
                            hour_label,
                            ping_count,
                            "" if summary is None else summary["min"],
                            "" if summary is None else summary["q1"],
                            "" if summary is None else summary["median"],
                            "" if summary is None else summary["q3"],
                            "" if summary is None else summary["max"],
                            stats["resets"],
                            ssid,
                            int(round(minutes)),
                        ]
                    )
            else:
                # No SSID info; still log one row
                writer.writerow(
                    [
                        date_str,
                        hour_label,
                        ping_count,
                        "" if summary is None else summary["min"],
                        "" if summary is None else summary["q1"],
                        "" if summary is None else summary["median"],
                        "" if summary is None else summary["q3"],
                        "" if summary is None else summary["max"],
                        stats["resets"],
                        "",
                        "",
                    ]
                )
    except Exception as e:
        print(f"Error writing hourly CSV log: {e}")


def _current_hour_key():
    now = datetime.now()
    return now.replace(minute=0, second=0, microsecond=0)


def record_latency(latency_ms):
    """Record a single successful ping latency (in ms) in the current hour."""
    if latency_ms is None:
        return
    key = _current_hour_key()
    stats = hourly_stats.setdefault(key, {"latencies": [], "resets": 0})
    stats["latencies"].append(latency_ms)


def record_reset():
    """Record that a WiFi reset was attempted in the current hour."""
    key = _current_hour_key()
    stats = hourly_stats.setdefault(key, {"latencies": [], "resets": 0})
    stats["resets"] += 1


def record_ssid_event(when, ssid):
    """Record that from `when` onward we are on `ssid` (until the next event)."""
    global current_ssid
    current_ssid = ssid or "unknown"
    reset_events.append((when, current_ssid))


def _hour_window(dt):
    start = dt.replace(minute=0, second=0, microsecond=0)
    end = start.replace(minute=59, second=59, microsecond=999999)
    return start, end


def _summarize_latencies(latencies):
    """Return min, q1, median, q3, max for a list of latencies."""
    if not latencies:
        return None
    values = sorted(latencies)
    n = len(values)

    def idx(p):
        # Simple percentile index: 0 <= index <= n-1
        return int(round((n - 1) * p))

    return {
        "min": values[0],
        "q1": values[idx(0.25)],
        "median": values[idx(0.5)],
        "q3": values[idx(0.75)],
        "max": values[-1],
    }


def print_last_hours_summary(max_hours=4):
    """
    Print a summary log for up to the last `max_hours` hour buckets,
    including the current (possibly partial) hour.
    """
    if not hourly_stats:
        print("\nNo latency statistics collected.")
        return

    current_hour = _current_hour_key()
    hours = sorted(h for h in hourly_stats.keys() if h <= current_hour)
    if not hours:
        print("\nNo latency statistics collected.")
        return

    # Last `max_hours` buckets: typically current hour + previous 3
    selected = hours[-max_hours:]

    print("\n================ Hourly Latency Summary ================")
    for hour in selected:
        stats = hourly_stats[hour]
        date_label = hour.strftime("%Y-%m-%d %H:00")
        summary = _summarize_latencies(stats["latencies"])

        # Compute SSID usage minutes for this hour
        hour_start = hour
        hour_end = hour_start.replace(minute=59, second=59, microsecond=999999)
        ssid_minutes = compute_ssid_minutes_for_hour(hour_start, hour_end)

        print(f"\n{date_label}")
        if summary:
            print(f"  pings: {len(stats['latencies'])}")
            print(
                "  latency (ms): "
                f"min={summary['min']} "
                f"q1={summary['q1']} "
                f"median={summary['median']} "
                f"q3={summary['q3']} "
                f"max={summary['max']}"
            )
        else:
            print("  pings: 0 (no successful latency measurements)")

        print(f"  wifi resets: {stats['resets']}")

        if ssid_minutes:
            print("  SSID usage (minutes):")
            for ssid, minutes in ssid_minutes.items():
                print(f"    {ssid}: {minutes:.1f}")
        else:
            print("  SSID usage: (no SSID data)")

        # Log to CSV as well
        log_hourly_to_csv(hour, stats, summary, ssid_minutes)

    print("========================================================\n")


def compute_ssid_minutes_for_hour(hour_start, hour_end):
    """
    Estimate how many minutes each SSID was used in [hour_start, hour_end],
    based only on reset events (and the initial SSID event).
    """
    if script_start_time is None or not reset_events:
        return {}

    # Ensure events are in chronological order
    events = sorted(reset_events, key=lambda e: e[0])

    # Determine SSID active at the start of this hour
    current_ssid = "unknown"
    for when, ssid in events:
        if when <= hour_start:
            current_ssid = ssid or "unknown"
        else:
            break

    # Don't attribute time beyond when the script actually stopped running
    effective_end = min(hour_end, datetime.now())

    # Start time for this hour's accounting
    cursor = max(script_start_time, hour_start)
    if cursor > effective_end:
        return {}

    minutes_per_ssid = {}

    # Iterate over events inside this hour
    for when, ssid in events:
        if when <= cursor:
            continue
        if when > effective_end:
            break

        # Time from cursor until this event belongs to current_ssid
        segment_end = when
        if segment_end > effective_end:
            segment_end = effective_end

        if segment_end > cursor:
            delta_minutes = (segment_end - cursor).total_seconds() / 60.0
            minutes_per_ssid[current_ssid] = minutes_per_ssid.get(current_ssid, 0.0) + delta_minutes

        # Move cursor and switch SSID
        cursor = when
        current_ssid = ssid or "unknown"

    # Final segment until end of hour (or script end)
    if cursor < effective_end:
        delta_minutes = (effective_end - cursor).total_seconds() / 60.0
        minutes_per_ssid[current_ssid] = minutes_per_ssid.get(current_ssid, 0.0) + delta_minutes

    # Filter out zero-minute entries
    return {k: v for k, v in minutes_per_ssid.items() if v > 0}


def finalize_hour_to_ssid_usage(hour_start, hour_end, ssid_usage, resets_this_hour):
    """
    Update persistent ssid_usage (hours_used, total_resets) from the provided hour range.
    - hours_used: accumulate minutes_per_ssid / 60.0
    - total_resets: add resets_this_hour counts
    """
    minutes_per_ssid = compute_ssid_minutes_for_hour(hour_start, hour_end)
    for ssid, minutes in minutes_per_ssid.items():
        entry = ssid_usage.setdefault(ssid, {"hours_used": 0.0, "total_resets": 0})
        entry["hours_used"] = float(entry.get("hours_used", 0.0)) + (minutes / 60.0)

    for ssid, count in resets_this_hour.items():
        entry = ssid_usage.setdefault(ssid, {"hours_used": 0.0, "total_resets": 0})
        entry["total_resets"] = int(entry.get("total_resets", 0)) + int(count)


def ping_host(host="www.google.com", timeout=3):
    """
    Ping a host and return True if successful, False otherwise.
    
    Args:
        host: Hostname or IP address to ping
        timeout: Timeout in seconds
    
    Returns:
        (success, latency_ms)
        - success: True if ping successful, False otherwise
        - latency_ms: round-trip time in milliseconds (int) or None if unavailable
    """
    try:
        # Windows ping command: ping -n 1 -w <timeout_ms> <host>
        # -n 1: send 1 packet
        # -w <timeout_ms>: timeout in milliseconds
        timeout_ms = timeout * 1000
        result = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), host],
            capture_output=True,
            text=True,
            timeout=timeout + 2
        )

        success = result.returncode == 0
        latency_ms = None

        if success:
            # Try to parse latency from the ping output
            # Typical Windows line: "    Minimum = 10ms, Maximum = 15ms, Average = 12ms"
            # Or per-reply line: "Reply from ... time=12ms ..."
            # We'll first look for "time=" pattern on reply line(s)
            for line in result.stdout.splitlines():
                if "time=" in line.lower():
                    match = re.search(r"time[=<]\s*([\d]+)ms", line, re.IGNORECASE)
                    if match:
                        latency_ms = int(match.group(1))
                        break

            # Fallback: try to parse "Average = Xms" from statistics section
            if latency_ms is None:
                avg_match = re.search(r"Average\s*=\s*([\d]+)ms", result.stdout, re.IGNORECASE)
                if avg_match:
                    latency_ms = int(avg_match.group(1))

        return success, latency_ms
    except subprocess.TimeoutExpired:
        return False, None
    except Exception as e:
        print(f"Error pinging {host}: {e}")
        return False, None


def ping_burst(host="www.google.com", count=5, timeout=3):
    """Ping `count` times back-to-back. Returns number of successes."""
    successes = 0
    for _ in range(count):
        ok, _lat = ping_host(host, timeout=timeout)
        if ok:
            successes += 1
    return successes


def get_wifi_profile_name():
    """
    Get the currently connected WiFi profile name.
    
    Returns:
        Profile name string or None if not found
    """
    try:
        # Get current WiFi connection info
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            # Look for SSID in the output
            match = re.search(r'SSID\s+:\s+(.+)', result.stdout)
            if match:
                ssid = match.group(1).strip()
                # Now get the profile name for this SSID
                profile_result = subprocess.run(
                    ["netsh", "wlan", "show", "profiles"],
                    capture_output=True,
                    text=True
                )
                if profile_result.returncode == 0:
                    # Find profile that matches the SSID
                    lines = profile_result.stdout.split('\n')
                    for i, line in enumerate(lines):
                        if ssid in line:
                            # Get the profile name from the previous line or current line
                            profile_match = re.search(r'All User Profile\s+:\s+(.+)', line)
                            if not profile_match and i > 0:
                                profile_match = re.search(r'All User Profile\s+:\s+(.+)', lines[i-1])
                            if profile_match:
                                return profile_match.group(1).strip()
        return None
    except Exception as e:
        print(f"Error getting WiFi profile: {e}")
        return None


def get_wifi_ssid():
    """
    Get the currently connected WiFi SSID.
    
    Returns:
        SSID string or None if not found
    """
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            match = re.search(r'SSID\s+:\s+(.+)', result.stdout)
            if match:
                return match.group(1).strip()
        return None
    except Exception as e:
        print(f"Error getting WiFi SSID: {e}")
        return None


def list_saved_wifi_profiles():
    """Return a set of saved WiFi profile names from Windows."""
    profiles = set()
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "profiles"],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            return profiles
        for line in result.stdout.splitlines():
            match = re.search(r"All User Profile\s*:\s*(.+)", line)
            if match:
                profiles.add(match.group(1).strip())
    except Exception as e:
        print(f"Error listing WiFi profiles: {e}")
    return profiles


def connect_to_wifi_profile(profile_name):
    """Attempt to connect to a saved WiFi profile by name."""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "connect", f"name={profile_name}"],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Error connecting to WiFi profile {profile_name}: {e}")
        return False


def reconnect_wifi():
    """
    Disconnect and reconnect WiFi.
    """
    print("\n" + "="*50)
    print("WiFi Reconnection Attempt")
    print("="*50)
    
    # Get current SSID
    ssid = get_wifi_ssid()
    if not ssid:
        print("Warning: Could not detect current WiFi SSID")
        print("Attempting to disconnect anyway...")
    else:
        print(f"Current WiFi SSID: {ssid}")
    
    # Disconnect WiFi
    print("\nDisconnecting WiFi...")
    try:
        result = subprocess.run(
            ["netsh", "wlan", "disconnect"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("WiFi disconnected successfully")
        else:
            print(f"Disconnect command output: {result.stdout}")
            print(f"Disconnect command error: {result.stderr}")
    except Exception as e:
        print(f"Error disconnecting WiFi: {e}")
        return False
    
    # Wait a moment before reconnecting
    print("\nWaiting 2 seconds before reconnecting...")
    time.sleep(2)
    
    # Reconnect WiFi
    print("Reconnecting WiFi...")
    try:
        if ssid:
            # Try to connect using SSID
            result = subprocess.run(
                ["netsh", "wlan", "connect", f"name={ssid}"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                print(f"WiFi reconnection initiated for: {ssid}")
            else:
                # If SSID connection fails, try getting profile name
                profile_name = get_wifi_profile_name()
                if profile_name:
                    result = subprocess.run(
                        ["netsh", "wlan", "connect", f"name={profile_name}"],
                        capture_output=True,
                        text=True
                    )
                    if result.returncode == 0:
                        print(f"WiFi reconnection initiated using profile: {profile_name}")
                    else:
                        print(f"Reconnection failed. Output: {result.stdout}")
                        print(f"Error: {result.stderr}")
                        return False
                else:
                    print("Could not determine profile name. Please reconnect manually.")
                    return False
        else:
            # Try to get profile name and connect
            profile_name = get_wifi_profile_name()
            if profile_name:
                result = subprocess.run(
                    ["netsh", "wlan", "connect", f"name={profile_name}"],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    print(f"WiFi reconnection initiated using profile: {profile_name}")
                else:
                    print(f"Reconnection failed. Output: {result.stdout}")
                    print(f"Error: {result.stderr}")
                    return False
            else:
                print("Could not determine WiFi profile. Please reconnect manually.")
                return False
        
        # Wait a bit for connection to establish
        print("\nWaiting 5 seconds for connection to establish...")
        time.sleep(5)
        
        # Verify connection (burst ping)
        successes = ping_burst(count=5)
        if successes >= 3:
            print(f"✓ Connection looks healthy ({successes}/5 pings succeeded)")
            return True
        else:
            print(f"⚠ Connection still looks poor ({successes}/5 pings succeeded)")
            return False
            
    except Exception as e:
        print(f"Error reconnecting WiFi: {e}")
        return False


def main():
    """
    Main loop: ping www.google.com and reconnect WiFi if:
    - 4 consecutive failures occur, or
    - 7 of the last 10 pings exceed the latency threshold (slow connection).
    """
    global script_start_time

    host = "www.google.com"
    consecutive_failures = 0
    required_failures = 6
    ping_interval = 5  # seconds between pings

    # Latency-based reconnect settings
    latency_threshold_ms = 300
    window_size = 20
    slow_threshold_count = 14
    latency_window = deque(maxlen=window_size)  # stores booleans: True if ping > threshold
    
    print("="*50)
    print("Auto Reconnect Script")
    print("="*50)
    print(f"Monitoring: {host}")
    print(f"Reconnect threshold (failures): {required_failures} consecutive failures")
    print(f"Reconnect threshold (latency): {slow_threshold_count} of last {window_size} pings > {latency_threshold_ms} ms")
    print(f"Ping interval: {ping_interval} seconds")
    print("="*50)
    print("\nPress Ctrl+C to stop\n")

    # Initialize script start time and initial SSID (baseline)
    script_start_time = datetime.now()
    initial_ssid = get_wifi_ssid()
    record_ssid_event(script_start_time, initial_ssid)

    # Load SSID usage statistics from previous runs
    ssid_usage = load_ssid_usage_csv()

    # Track resets per SSID within the current hour
    resets_this_hour = {}

    # Ping counter for sampling SSID every N pings
    ping_counter = 0
    ssid_sample_every_pings = 12

    # Schedule periodic SSID checks on the hour
    hour_start = script_start_time.replace(minute=0, second=0, microsecond=0)
    if script_start_time == hour_start:
        next_ssid_check = hour_start
    else:
        next_ssid_check = hour_start + timedelta(hours=1)

    # Schedule hourly SSID usage finalization on the hour
    current_hour_start, current_hour_end = _hour_window(script_start_time)
    next_hourly_finalize = current_hour_start + timedelta(hours=1)

    try:
        while True:
            now = datetime.now()

            # Finalize SSID usage once per hour boundary
            if now >= next_hourly_finalize:
                prev_hour_start = next_hourly_finalize - timedelta(hours=1)
                prev_hour_end = prev_hour_start.replace(minute=59, second=59, microsecond=999999)
                finalize_hour_to_ssid_usage(prev_hour_start, prev_hour_end, ssid_usage, resets_this_hour)
                write_ssid_usage_csv(ssid_usage)
                resets_this_hour = {}
                next_hourly_finalize = next_hourly_finalize + timedelta(hours=1)

            # Periodic SSID check each hour
            if now >= next_ssid_check:
                ssid_now = get_wifi_ssid()
                if ssid_now and ssid_now != current_ssid:
                    record_ssid_event(now, ssid_now)
                next_ssid_check = next_ssid_check + timedelta(hours=1)

            # SSID sampling every N pings (only record if it changed)
            ping_counter += 1
            if ping_counter % ssid_sample_every_pings == 0:
                ssid_now = get_wifi_ssid()
                if ssid_now and ssid_now != current_ssid:
                    record_ssid_event(now, ssid_now)

            # Ping the host
            success, latency_ms = ping_host(host)

            # Track latency only for successful pings where we have a measurement
            if success and latency_ms is not None:
                is_slow = latency_ms > latency_threshold_ms
                latency_window.append(is_slow)
                record_latency(latency_ms)
            elif success:
                # Successful ping but no latency parsed; treat as not-slow to avoid false triggers
                is_slow = False
                latency_window.append(False)
            else:
                # On failures we still want to track that the connection is bad.
                # Treat failures as "slow" for the purpose of latency window, so
                # intermittent failures don't hide poor connectivity.
                is_slow = True
                latency_window.append(True)

            reset_reason = None

            if success:
                if consecutive_failures > 0:
                    if latency_ms is not None:
                        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✓ Ping ({latency_ms} ms) (recovered from {consecutive_failures} failures)")
                    else:
                        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✓ Ping (recovered from {consecutive_failures} failures)")
                else:
                    if latency_ms is not None:
                        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✓ Ping ({latency_ms} ms)")
                    else:
                        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✓ Ping ")
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✗ Failed ({consecutive_failures}/{required_failures} consecutive_failures)")
                
                # If we've reached the threshold, reconnect WiFi
                if consecutive_failures >= required_failures:
                    print(f"\n⚠ {consecutive_failures} consecutive failures!")
                    reconnect_wifi()
                    record_reset()
                    # Record new SSID from this point forward
                    record_ssid_event(datetime.now(), get_wifi_ssid())
                    resets_this_hour[current_ssid] = resets_this_hour.get(current_ssid, 0) + 1
                    ssid_usage.setdefault(current_ssid, {"hours_used": 0.0, "total_resets": 0})
                    ssid_usage[current_ssid]["total_resets"] = int(ssid_usage[current_ssid].get("total_resets", 0)) + 1
                    write_ssid_usage_csv(ssid_usage)
                    reset_reason = "failures"
                    consecutive_failures = 0  # Reset counter after reconnection attempt
                    latency_window.clear()
                    print("\n" + "="*50)
                    print("Resuming monitoring...")
                    print("="*50 + "\n")

            # Check latency-based reconnect condition (7 of last 10 pings slow)
            if len(latency_window) == window_size:
                slow_count = sum(1 for is_slow_entry in latency_window if is_slow_entry)
                if slow_count >= slow_threshold_count:
                    print(f"\n⚠ Slow connection: {slow_count} of last {window_size} pings exceeded {latency_threshold_ms} ms")
                    reconnect_wifi()
                    record_reset()
                    # Record new SSID from this point forward
                    record_ssid_event(datetime.now(), get_wifi_ssid())
                    resets_this_hour[current_ssid] = resets_this_hour.get(current_ssid, 0) + 1
                    ssid_usage.setdefault(current_ssid, {"hours_used": 0.0, "total_resets": 0})
                    ssid_usage[current_ssid]["total_resets"] = int(ssid_usage[current_ssid].get("total_resets", 0)) + 1
                    write_ssid_usage_csv(ssid_usage)
                    reset_reason = "latency"
                    consecutive_failures = 0
                    latency_window.clear()
                    print("\n" + "="*50)
                    print("Resuming monitoring...")
                    print("="*50 + "\n")

            # If we've reset the current SSID 3 times within this hour, try top 5 SSIDs
            hour_key = _current_hour_key()
            current_ssid_resets = resets_this_hour.get(current_ssid, 0)
            if current_ssid_resets >= 3:
                print(f"\n⚠ SSID '{current_ssid}' has been reset {current_ssid_resets} times this hour ({hour_key.strftime('%Y-%m-%d %H:00')}). Trying top SSIDs...")

                saved_profiles = list_saved_wifi_profiles()
                # Rank by hours_used desc
                ranked = sorted(
                    ssid_usage.items(),
                    key=lambda kv: (kv[1].get("hours_used", 0.0), kv[1].get("total_resets", 0), kv[0]),
                    reverse=True,
                )
                candidates = [ssid for ssid, _v in ranked if ssid in saved_profiles and ssid != current_ssid][:5]

                switched = False
                for candidate in candidates:
                    print(f"Trying SSID/profile: {candidate}")
                    if not connect_to_wifi_profile(candidate):
                        print("  connect failed")
                        continue
                    time.sleep(5)
                    successes = ping_burst(host=host, count=5)
                    print(f"  ping burst: {successes}/5")
                    if successes >= 3:
                        record_ssid_event(datetime.now(), candidate)
                        consecutive_failures = 0
                        latency_window.clear()
                        switched = True
                        print(f"✓ Switched to {candidate}")
                        break

                # Avoid retrying failover every loop once threshold reached
                if switched:
                    resets_this_hour[current_ssid] = 0
                else:
                    resets_this_hour[current_ssid] = max(resets_this_hour.get(current_ssid, 0), 3)

            # Wait before next ping
            time.sleep(ping_interval)
            
    except KeyboardInterrupt:
        print("\n\nScript stopped by user.")
        print_last_hours_summary(max_hours=4)
        sys.exit(0)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()



