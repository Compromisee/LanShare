#!/usr/bin/env python3
"""
LAN Share - Blazing Fast Multi-Threaded LAN File Explorer & Transfer
"""

import os
import sys
import json
import time
import socket
import struct
import hashlib
import logging
import mimetypes
import threading
import ipaddress
import subprocess
from pathlib import Path
from datetime import datetime
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote
import http.client

# ── Config ──────────────────────────────────────────────────────────────────
BASE_DIR      = Path.home()          # Root for file browsing (home directory)
STATE_FILE    = Path("state.json")
DASHBOARD_HTML = Path("dashboard.html")
DISCOVERY_PORT = 54321               # UDP broadcast port
HTTP_PORT      = 8765                # HTTP server port
SCAN_INTERVAL  = 5                   # Seconds between network scans
BROADCAST_INT  = 3                   # Seconds between presence broadcasts
MAX_WORKERS    = 64                  # Thread pool size
CHUNK_SIZE     = 1 << 20            # 1 MB chunks for file transfer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lan_share")

# ── Shared State ─────────────────────────────────────────────────────────────
state_lock   = threading.RLock()
transfer_lock = threading.Lock()

state = {
    "devices": {},          # ip -> device info
    "transfers": {},        # transfer_id -> transfer info
    "my_ip": "",
    "hostname": socket.gethostname(),
    "started": datetime.now().isoformat(),
    "stats": {"sent_bytes": 0, "recv_bytes": 0, "files_sent": 0, "files_recv": 0},
}

event_queue = Queue(maxsize=500)   # SSE events
executor    = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="lan")


# ── Utility ──────────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    """Fastest way to find primary LAN IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_subnet(ip: str) -> str:
    """Return the /24 subnet base, e.g. '192.168.1'."""
    parts = ip.split(".")
    return ".".join(parts[:3])


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def push_event(kind: str, data: dict):
    try:
        event_queue.put_nowait({"type": kind, "data": data, "ts": time.time()})
    except Exception:
        pass


def save_state():
    with state_lock:
        snap = {
            "devices": state["devices"],
            "my_ip": state["my_ip"],
            "hostname": state["hostname"],
            "started": state["started"],
            "stats": state["stats"],
            "transfers": {
                tid: {k: v for k, v in t.items() if k != "_file"}
                for tid, t in state["transfers"].items()
            },
        }
    try:
        STATE_FILE.write_text(json.dumps(snap, indent=2))
    except Exception as e:
        log.warning(f"save_state: {e}")


# ── Device Discovery ──────────────────────────────────────────────────────────

def build_presence_packet() -> bytes:
    info = {
        "hostname": state["hostname"],
        "ip": state["my_ip"],
        "port": HTTP_PORT,
        "version": "1.0",
    }
    payload = json.dumps(info).encode()
    return b"LANSHARE:" + payload


def udp_broadcaster():
    """Broadcast presence every BROADCAST_INT seconds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    packet = build_presence_packet()
    while True:
        try:
            sock.sendto(packet, ("<broadcast>", DISCOVERY_PORT))
        except Exception as e:
            log.debug(f"broadcast error: {e}")
        time.sleep(BROADCAST_INT)


def udp_listener():
    """Listen for presence packets from other devices."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass
    sock.bind(("", DISCOVERY_PORT))
    sock.settimeout(2)
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            if data.startswith(b"LANSHARE:"):
                payload = data[9:]
                info = json.loads(payload)
                ip = info.get("ip", addr[0])
                register_device(ip, info)
        except socket.timeout:
            pass
        except Exception as e:
            log.debug(f"udp_listener: {e}")


def probe_host(ip: str) -> dict | None:
    """Try HTTP ping to LAN Share port."""
    try:
        conn = http.client.HTTPConnection(ip, HTTP_PORT, timeout=1)
        conn.request("GET", "/api/info")
        r = conn.getresponse()
        if r.status == 200:
            data = json.loads(r.read())
            conn.close()
            return data
    except Exception:
        pass
    return None


def ping_host(ip: str) -> bool:
    """Fast ICMP-style check: try TCP connect to common ports."""
    for port in (HTTP_PORT, 22, 80, 443, 445):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.3)
            if s.connect_ex((ip, port)) == 0:
                s.close()
                return True
            s.close()
        except Exception:
            pass
    return False


def register_device(ip: str, info: dict):
    hostname = info.get("hostname", ip)
    port     = info.get("port", HTTP_PORT)
    with state_lock:
        existing = state["devices"].get(ip, {})
        is_new   = ip not in state["devices"]
        state["devices"][ip] = {
            "ip": ip,
            "hostname": hostname,
            "port": port,
            "last_seen": datetime.now().isoformat(),
            "status": "online",
            "is_self": ip == state["my_ip"],
            "version": info.get("version", "?"),
        }
    if is_new:
        log.info(f"New device: {hostname} ({ip})")
        push_event("device_joined", state["devices"][ip])
        save_state()
    else:
        push_event("device_updated", state["devices"][ip])


def network_scanner():
    """Periodically scan the /24 subnet for active hosts running LAN Share."""
    time.sleep(5)  # let the server start first
    while True:
        my_ip  = state["my_ip"]
        subnet = get_subnet(my_ip)
        ips    = [f"{subnet}.{i}" for i in range(1, 255) if f"{subnet}.{i}" != my_ip]

        futures = {executor.submit(probe_host, ip): ip for ip in ips}
        for fut in as_completed(futures):
            ip = futures[fut]
            try:
                info = fut.result()
                if info:
                    register_device(ip, info)
            except Exception:
                pass

        # Mark stale devices offline
        cutoff = time.time() - (SCAN_INTERVAL * 3)
        with state_lock:
            for ip, dev in state["devices"].items():
                try:
                    ts = datetime.fromisoformat(dev["last_seen"]).timestamp()
                    if ts < cutoff and dev["status"] == "online":
                        dev["status"] = "offline"
                        push_event("device_left", dev)
                except Exception:
                    pass

        save_state()
        time.sleep(SCAN_INTERVAL)


# ── File Browsing ─────────────────────────────────────────────────────────────

def list_directory(path_str: str) -> dict:
    """Return directory listing as JSON-serializable dict."""
    try:
        path = Path(path_str).resolve()
        if not path.exists():
            return {"error": "Path not found"}
        if not path.is_dir():
            return {"error": "Not a directory"}

        entries = []
        try:
            items = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return {"error": "Permission denied"}

        for item in items:
            try:
                stat  = item.stat()
                entry = {
                    "name": item.name,
                    "path": str(item),
                    "is_dir": item.is_dir(),
                    "size": stat.st_size if not item.is_dir() else 0,
                    "size_human": human_size(stat.st_size) if not item.is_dir() else "—",
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "mime": mimetypes.guess_type(item.name)[0] or "application/octet-stream",
                }
                entries.append(entry)
            except (PermissionError, OSError):
                continue

        return {
            "path": str(path),
            "parent": str(path.parent) if path != path.parent else None,
            "entries": entries,
            "count": len(entries),
        }
    except Exception as e:
        return {"error": str(e)}


# ── File Transfer ─────────────────────────────────────────────────────────────

def make_transfer_id() -> str:
    return hashlib.md5(f"{time.time()}{threading.get_ident()}".encode()).hexdigest()[:12]


def do_upload(target_ip: int, target_port: int, src_path: str, dst_dir: str, tid: str):
    """Upload a file to a remote device."""
    src = Path(src_path)
    total = src.stat().st_size

    with transfer_lock:
        state["transfers"][tid] = {
            "id": tid, "direction": "send", "file": src.name,
            "path": src_path, "target": target_ip,
            "total": total, "done": 0, "status": "transferring",
            "started": datetime.now().isoformat(),
        }
    push_event("transfer_start", state["transfers"][tid])

    try:
        url_path = f"/api/receive?dst={quote(dst_dir)}&filename={quote(src.name)}&size={total}"
        conn = http.client.HTTPConnection(target_ip, target_port, timeout=30)
        conn.putrequest("POST", url_path)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(total))
        conn.endheaders()

        sent = 0
        t0   = time.time()
        with open(src, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                conn.send(chunk)
                sent += len(chunk)
                with transfer_lock:
                    state["transfers"][tid]["done"] = sent
                    state["transfers"][tid]["speed"] = sent / max(time.time() - t0, 0.001)
                push_event("transfer_progress", {
                    "id": tid, "done": sent, "total": total,
                    "pct": round(sent / total * 100, 1)
                })

        r = conn.getresponse()
        ok = r.status == 200
        conn.close()

        with state_lock:
            state["transfers"][tid]["status"] = "done" if ok else "error"
            if ok:
                state["stats"]["sent_bytes"] += total
                state["stats"]["files_sent"] += 1

        push_event("transfer_done", {"id": tid, "ok": ok})
        save_state()
    except Exception as e:
        log.error(f"upload {tid}: {e}")
        with transfer_lock:
            state["transfers"][tid]["status"] = "error"
            state["transfers"][tid]["error"] = str(e)
        push_event("transfer_done", {"id": tid, "ok": False, "error": str(e)})


# ── HTTP Server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # silence default access log

    # ── routing ─────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed  = urlparse(self.path)
        path    = parsed.path
        params  = parse_qs(parsed.query)

        routes = {
            "/":              self._dashboard,
            "/dashboard.html": self._dashboard,
            "/api/info":      self._info,
            "/api/state":     self._state,
            "/api/browse":    self._browse,
            "/api/download":  self._download,
            "/api/events":    self._sse,
            "/api/remote/browse": self._remote_browse,
        }
        handler = routes.get(path)
        if handler:
            handler(params)
        else:
            self._404()

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/receive":
            self._receive(params)
        elif path == "/api/send":
            self._send(params)
        elif path == "/api/delete":
            self._delete(params)
        elif path == "/api/mkdir":
            self._mkdir(params)
        else:
            self._404()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _404(self, *_):
        self._json({"error": "not found"}, 404)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    # ── endpoints ────────────────────────────────────────────────────────────

    def _dashboard(self, *_):
        html = DASHBOARD_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _info(self, *_):
        self._json({
            "hostname": state["hostname"],
            "ip": state["my_ip"],
            "port": HTTP_PORT,
            "version": "1.0",
        })

    def _state(self, *_):
        with state_lock:
            self._json(dict(state))

    def _browse(self, params):
        path = params.get("path", [str(BASE_DIR)])[0]
        self._json(list_directory(path))

    def _download(self, params):
        path_str = params.get("path", [""])[0]
        path     = Path(path_str)
        if not path.exists() or not path.is_file():
            return self._json({"error": "file not found"}, 404)
        size = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(size))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        t0   = time.time()
        sent = 0
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                self.wfile.write(chunk)
                sent += len(chunk)
        with state_lock:
            state["stats"]["sent_bytes"] += sent
        log.info(f"download {path.name} ({human_size(size)}) in {time.time()-t0:.2f}s")

    def _receive(self, params):
        dst_dir  = params.get("dst", [str(Path.home() / "Downloads")])[0]
        filename = params.get("filename", ["upload"])[0]
        size     = int(params.get("size", [0])[0])
        dst_path = Path(dst_dir) / filename
        dst_path.parent.mkdir(parents=True, exist_ok=True)

        tid = make_transfer_id()
        with transfer_lock:
            state["transfers"][tid] = {
                "id": tid, "direction": "recv", "file": filename,
                "path": str(dst_path), "total": size, "done": 0,
                "status": "transferring", "started": datetime.now().isoformat(),
            }
        push_event("transfer_start", state["transfers"][tid])

        t0   = time.time()
        recv = 0
        try:
            with open(dst_path, "wb") as f:
                remaining = size
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    recv += len(chunk)
                    remaining -= len(chunk)
                    with transfer_lock:
                        state["transfers"][tid]["done"] = recv
                    push_event("transfer_progress", {
                        "id": tid, "done": recv, "total": size,
                        "pct": round(recv / size * 100, 1) if size else 100
                    })
            with state_lock:
                state["transfers"][tid]["status"] = "done"
                state["stats"]["recv_bytes"] += recv
                state["stats"]["files_recv"] += 1
            push_event("transfer_done", {"id": tid, "ok": True})
            save_state()
            log.info(f"received {filename} ({human_size(recv)}) in {time.time()-t0:.2f}s")
            self._json({"ok": True, "path": str(dst_path)})
        except Exception as e:
            log.error(f"receive {tid}: {e}")
            with transfer_lock:
                state["transfers"][tid]["status"] = "error"
            push_event("transfer_done", {"id": tid, "ok": False, "error": str(e)})
            self._json({"error": str(e)}, 500)

    def _send(self, params):
        """Trigger upload to remote device."""
        body = json.loads(self._read_body() or b"{}")
        src  = body.get("src_path")
        ip   = body.get("target_ip")
        port = int(body.get("target_port", HTTP_PORT))
        dst  = body.get("dst_dir", str(Path.home() / "Downloads"))
        if not src or not ip:
            return self._json({"error": "missing src_path or target_ip"}, 400)
        tid = make_transfer_id()
        executor.submit(do_upload, ip, port, src, dst, tid)
        self._json({"ok": True, "transfer_id": tid})

    def _remote_browse(self, params):
        """Proxy a browse request to a remote device."""
        ip   = params.get("ip", [""])[0]
        port = int(params.get("port", [HTTP_PORT])[0])
        path = params.get("path", [str(Path.home())])[0]
        try:
            conn = http.client.HTTPConnection(ip, port, timeout=5)
            conn.request("GET", f"/api/browse?path={quote(path)}")
            r    = conn.getresponse()
            data = json.loads(r.read())
            conn.close()
            self._json(data)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _delete(self, params):
        body = json.loads(self._read_body() or b"{}")
        path = Path(body.get("path", ""))
        if not path.exists():
            return self._json({"error": "not found"}, 404)
        try:
            if path.is_dir():
                import shutil
                shutil.rmtree(path)
            else:
                path.unlink()
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _mkdir(self, params):
        body = json.loads(self._read_body() or b"{}")
        path = Path(body.get("path", ""))
        try:
            path.mkdir(parents=True, exist_ok=True)
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _sse(self, *_):
        """Server-Sent Events stream for live updates."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        # send initial state snapshot
        self._sse_write("init", state)

        while True:
            try:
                event = event_queue.get(timeout=20)
                self._sse_write(event["type"], event["data"])
            except Empty:
                self._sse_write("ping", {"ts": time.time()})  # keepalive
            except (BrokenPipeError, ConnectionResetError):
                break
            except Exception:
                break

    def _sse_write(self, event: str, data):
        try:
            payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
            encoded = payload.encode()
            chunk   = f"{len(encoded):x}\r\n".encode() + encoded + b"\r\n"
            self.wfile.write(chunk)
            self.wfile.flush()
        except Exception:
            raise


class ThreadedHTTPServer(HTTPServer):
    """Handle every request in a thread from the pool."""
    daemon_threads = True

    def process_request(self, request, client_address):
        executor.submit(self.process_request_thread, request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    state["my_ip"] = get_local_ip()
    log.info(f"My IP: {state['my_ip']}  Hostname: {state['hostname']}")

    # Register self
    register_device(state["my_ip"], {
        "hostname": state["hostname"],
        "ip": state["my_ip"],
        "port": HTTP_PORT,
    })

    # Background threads
    threads = [
        threading.Thread(target=udp_broadcaster, daemon=True, name="broadcaster"),
        threading.Thread(target=udp_listener,    daemon=True, name="listener"),
        threading.Thread(target=network_scanner, daemon=True, name="scanner"),
    ]
    for t in threads:
        t.start()

    # HTTP server
    server = ThreadedHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    log.info(f"Dashboard → http://{state['my_ip']}:{HTTP_PORT}/")
    log.info(f"Also try  → http://localhost:{HTTP_PORT}/")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down…")
        server.shutdown()


if __name__ == "__main__":
    main()
