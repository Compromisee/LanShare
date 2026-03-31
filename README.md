# 🚀 LAN Share — Blazing Fast LAN File Explorer & Transfer

A high-performance, multi-threaded LAN file sharing tool with a live browser dashboard.  
Browse any computer's files on your local network, send/receive files, and monitor everything in real time.

---

## Features

- **Auto device discovery** — UDP broadcast + active /24 subnet scanning detects devices in seconds
- **Blazing fast** — 64-thread pool for concurrent connections, 1 MB chunk transfers, keep-alive HTTP
- **Live dashboard** — Server-Sent Events push updates to the browser instantly, no polling
- **File explorer** — Browse any connected device's filesystem like a native file manager
- **Send & receive** — Transfer files to/from any device with a progress bar
- **Self-browse** — Works even with a single machine (browse your own files)
- **3-file architecture** — `server.py`, `state.json`, `dashboard.html`

---

## Files

| File | Purpose |
|------|---------|
| `server.py` | Python backend — HTTP server, UDP discovery, file transfer engine |
| `dashboard.html` | Live browser UI — file explorer, device list, transfer tracker |
| `state.json` | Persisted state — devices seen, transfer history, stats |

---

## Requirements

- Python 3.10+ (uses walrus operator `:=`)
- No external packages — 100% standard library

---

## Quick Start

```bash
# Clone or copy the 3 files into a folder, then:
python server.py
```

Open your browser to the URL printed in the terminal, e.g.:

```
Dashboard → http://192.168.1.42:8765/
```

Run the same command on every machine you want to connect.  
They will discover each other automatically within a few seconds.

---

## How It Works

### Discovery (two-layer)

1. **UDP Broadcast** — Every device broadcasts a `LANSHARE:` packet every 3 seconds on port `54321`.  
   Devices on the same subnet hear it instantly.

2. **Active Subnet Scan** — A background thread probes every IP in your `/24` subnet (`x.x.x.1`–`x.x.x.254`)  
   by hitting the LAN Share HTTP endpoint. New devices appear in seconds.

### File Transfer

- Click a remote device in the sidebar → its filesystem opens in the explorer
- Select files → click **⇧ Send Selected** → choose target device + destination folder
- Progress bars update live via SSE

### Single-Device Mode

Works perfectly on one machine — browse your own files, test transfers to yourself,  
or just use it as a fast local file manager in the browser.

---

## Configuration

Edit the constants at the top of `server.py`:

```python
BASE_DIR       = Path.home()   # Root directory for browsing
HTTP_PORT      = 8765          # Web server port
DISCOVERY_PORT = 54321         # UDP broadcast port
SCAN_INTERVAL  = 5             # Seconds between network scans
BROADCAST_INT  = 3             # Seconds between presence broadcasts
MAX_WORKERS    = 64            # Thread pool size
CHUNK_SIZE     = 1 << 20      # 1 MB transfer chunks
```

---

## API Reference

All endpoints are available on every running instance.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve the dashboard |
| GET | `/api/info` | Device info (used for discovery probing) |
| GET | `/api/state` | Full JSON state snapshot |
| GET | `/api/browse?path=<path>` | List directory contents |
| GET | `/api/download?path=<path>` | Download a file |
| GET | `/api/events` | SSE stream for live updates |
| GET | `/api/remote/browse?ip=&port=&path=` | Proxy browse to remote device |
| POST | `/api/send` | Trigger upload to remote device |
| POST | `/api/receive` | Receive an uploaded file |
| POST | `/api/delete` | Delete a file or folder |
| POST | `/api/mkdir` | Create a directory |

---

## Performance Notes

- The thread pool (`MAX_WORKERS=64`) handles all HTTP requests and background scans concurrently
- File transfers use chunked streaming — no full file buffering in RAM
- SSE keeps one persistent connection open per browser tab; all events are pushed zero-latency
- Subnet scanning runs 254 probe connections in parallel via `ThreadPoolExecutor`

---

## Security

⚠️ **LAN only.** This tool has **no authentication**. Anyone on your local network can browse  
and download files from the directories you expose. Do not run on public/untrusted networks.

---

## License

MIT — use freely, modify freely.
