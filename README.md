## Auto WiFi Reconnect (Windows)

**Automatically monitors connectivity to `www.google.com` and resets WiFi when the connection becomes bad or fails.**

### Why this exists

In an office with multiple WiFi hotspots, the connection often becomes slow or flaky until manually disconnecting and reconnecting. While this likely indicates a deeper network issue, a manual reconnect typically restores a good connection. This small tool automates that manual step so you don’t have to think about it.

### What the script does

- **Continuously pings** `www.google.com` every few seconds.
- **Tracks failures**:
  - If **4 pings in a row fail**, it assumes the connection is broken and resets WiFi.
- **Tracks latency/slow connection**:
  - Measures round‑trip time (RTT) for each ping.
  - Maintains a **rolling window of the last 10 pings**.
  - If **7 out of the last 10 pings** take **more than 300 ms** (or fail), it assumes the connection is poor and resets WiFi.
- **Resets WiFi** (Windows only) using `netsh`:
  - Disconnects from the current WiFi network.
  - Waits a few seconds.
  - Reconnects to the same SSID/profile.
  - Verifies connectivity with another ping.

### Requirements and limitations

- **OS**: Windows (uses `ping` and `netsh wlan` commands).
- **Privileges**: You typically need to run the terminal **as Administrator** for `netsh` to manage WiFi.
- **Network**: Assumes you normally have a working WiFi profile configured in Windows.

### Installation

1. Clone or copy this repository into a folder, e.g.:

```bash
git clone [https://github.com/yoderz/autoReconnect](https://github.com/yoderz/autoReconnect) autoReconnect
cd autoReconnect
```

2. Ensure you have **Python 3** installed and on your `PATH`.

No extra Python packages are required; only the standard library is used.

### Usage

From an **elevated** Command Prompt or PowerShell (Run as Administrator), in the repo directory:

```bash
python auto_reconnect.py
```

The script will:

- Print each ping result with a timestamp and (when available) latency in milliseconds.
- Show counters for consecutive failures.
- Log when it decides to reset WiFi, then show “Resuming monitoring…” afterward.

Stop the script at any time with **Ctrl+C**.

### Configuration

To tweak the behavior, open `auto_reconnect.py` and adjust:

- **Ping target**:
  - `host = "www.google.com"` in `main()`.
- **Ping interval**:
  - `ping_interval = 5` (seconds between pings).
- **Failure-based reconnect**:
  - `required_failures = 4` (number of consecutive failed pings before a reset).
- **Latency-based reconnect**:
  - `latency_threshold_ms = 300` (what counts as “slow”).
  - `window_size = 10` (how many pings in the rolling window).
  - `slow_threshold_count = 7` (how many of those must be slow to trigger a reset).

### How it works (high level)

- Uses the Windows `ping` command to:
  - Check if the host is reachable.
  - Parse the RTT from the output where possible.
- Uses `netsh wlan show interfaces` and `netsh wlan show profiles` to:
  - Detect the current SSID and matching WiFi profile name.
- Uses `netsh wlan disconnect` and `netsh wlan connect name=<ssid/profile>` to:
  - Disconnect and reconnect to WiFi.

If reconnecting fails (e.g. no known profile or network is down), the script will log errors so you can troubleshoot, but it will not try destructive operations.

### Caveats and notes

- This is a **workaround**, not a true fix for underlying network issues.
- Frequent disconnect/reconnect cycles may briefly interrupt any active connections (VPN, RDP, file transfers, etc.).
- If your environment has strict policies around WiFi management, confirm that using `netsh wlan` is acceptable.

