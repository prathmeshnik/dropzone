#!/usr/bin/env python3
"""
Real-world upload test against a live server, through a fault-injecting proxy.

Two things are being tested:

1. REAL FILES. Upload each file in testfiles/ through the actual client
   JavaScript and verify sha256 end to end.

2. REAL FAILURE. The proxy deliberately truncates chunk PUTs mid-body, which
   is precisely what Cloudflare does when a request exceeds its ~100s timeout.
   Before this was fixed, a truncated chunk killed the whole upload. The
   upload must now survive it and still produce a byte-identical file.

Usage:
  python3 test_reality.py                 # spawns a throwaway server itself
  python3 test_reality.py <base-url> [key]  # or point it at a running one
"""
import hashlib
import http.server
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the server lives one level up
SRV = os.path.join(ROOT, "dropzone.py")
TESTFILES = os.path.join(HERE, "testfiles")
BASE = sys.argv[1] if len(sys.argv) > 1 else ""
KEY = sys.argv[2] if len(sys.argv) > 2 else ""

PROXY_PORT = 9123
TRUNCATE_EVERY = 3   # cut off every Nth chunk PUT

# Realistic filenames, including the awkward ones (unicode, spaces, double
# extensions) that have historically broken this server.
FIXTURES = [
    ("2026-09-28 22-19-09.mkv", 48 * 1024 * 1024),
    ("2026-09-30 00-41-04.mkv", 64 * 1024 * 1024),
    ("Holiday Photos 2024 (final).tar.gz", 12 * 1024 * 1024),
    ("IMG_0421.jpg", 5 * 1024 * 1024),
    ("report_2026.Q3.pdf", 800 * 1024),
    ("日本語のファイル名.bin", 3 * 1024 * 1024),
]


def ensure_fixtures(force=False):
    """Generate any missing test files on demand.

    ~130 MiB of random data is not worth keeping on disk permanently, so the
    fixtures are recreated rather than committed.
    """
    os.makedirs(TESTFILES, exist_ok=True)
    made = 0
    for name, size in FIXTURES:
        p = os.path.join(TESTFILES, name)
        if force or not os.path.exists(p) or os.path.getsize(p) != size:
            with open(p, "wb") as f:
                f.write(b"%PDF-1.7\n" if name.endswith(".pdf") else b"")
                left = size - f.tell()
                block = os.urandom(1 << 20)
                while left > 0:
                    n = min(left, len(block))
                    f.write(block[:n])
                    left -= n
            made += 1
    return made


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class FaultProxy(http.server.BaseHTTPRequestHandler):
    """Forwards to the real server, optionally severing chunk uploads."""

    protocol_version = "HTTP/1.1"
    server_version = "faultproxy"
    cut_count = 0

    def log_message(self, *a):
        pass

    def _forward(self, method):
        from urllib.parse import urlsplit
        u = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""

        is_chunk = method == "PUT" and u.path.startswith("/api/chunk/")
        should_cut = False
        if is_chunk and length > 0 and random.random() < 0.34:
            FaultProxy.cut_count += 1
            should_cut = True

        # Talking to the real server directly, bypassing the proxy entirely.
        hostport = BASE.split("//", 1)[1]
        host, _, port = hostport.partition(":")
        conn = http.client.HTTPConnection(host, int(port or 80), timeout=300)
        fwd = {k: v for k, v in self.headers.items()
               if k.lower() not in ("host", "content-length", "connection")}
        fwd["Host"] = host
        fwd["Content-Length"] = str(length)
        conn.request(method, self.path, body=body, headers=fwd)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()

        if should_cut:
            # Simulate Cloudflare giving up: send nothing, close mid-stream.
            try:
                self.wfile.write(b"HTTP/1.1 408 Request Timeout\r\nContent-Length: 0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return

        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.close_connection = True

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")

    def do_PUT(self):
        self._forward("PUT")

    def do_DELETE(self):
        self._forward("DELETE")


def run_client(url, key, size, name):
    cmd = ["node", os.path.join(HERE, "test_client.js"), url, key, str(size), name]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    return p.returncode, p.stdout + p.stderr


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def spawn_server():
    """Start a throwaway dropzone on a free port with its own storage dir.

    Without this the test only works if you remember to start a server first,
    which is a poor default for anyone running it from a fresh clone.
    """
    port = free_port()
    key = "reality-test-%d" % random.getrandbits(32)
    root = tempfile.mkdtemp(prefix="dz-reality-")
    proc = subprocess.Popen(
        [sys.executable, SRV, "--port", str(port), "--key", key, "--dir", root],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    base = "http://127.0.0.1:%d" % port

    import urllib.request
    for _ in range(100):
        if proc.poll() is not None:
            sys.stderr.write("server exited early:\n%s\n" % (proc.stderr.read() or ""))
            raise SystemExit(1)
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=1) as r:
                if r.read() == b"ok":
                    return proc, base, key, root
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise SystemExit("server never became healthy on port %d" % port)


def main():
    import http.client

    # FaultProxy reads BASE at module scope, so the spawned values must be
    # published globally rather than shadowed by locals.
    global BASE, KEY

    server = None
    if not BASE:
        server, BASE, KEY, root = spawn_server()
        print("spawned dropzone on %s (storage %s)\n" % (BASE, root))

    made = ensure_fixtures()
    if made:
        print("generated %d test file(s) in %s\n" % (made, TESTFILES))

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), FaultProxy)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proxy_url = "http://127.0.0.1:%d" % PROXY_PORT
    print("fault-injecting proxy on %s -> %s" % (proxy_url, BASE))
    print("severing roughly 1 in 3 chunk uploads mid-body\n")

    files = sorted(f for f in os.listdir(TESTFILES) if os.path.isfile(os.path.join(TESTFILES, f)))
    ok = fail = 0
    for f in files:
        path = os.path.join(TESTFILES, f)
        size = os.path.getsize(path)
        want = sha(path)
        print("%-42s %7.1f MiB" % (f, size / 1048576), flush=True)
        rc, out = run_client(proxy_url, KEY, size, path)
        got = ""
        for line in out.splitlines():
            if line.startswith("server sha256"):
                got = line.split(":", 1)[1].strip()
        good = rc == 0 and got == want
        print("   %s  sha %s" % ("PASS" if good else "FAIL", (got or "?")[:16]))
        if "RESULT: PASS" in out and good:
            ok += 1
        else:
            fail += 1
            for line in out.splitlines()[-8:]:
                print("      | " + line)
    print("\nserver-side severed chunks: %d" % FaultProxy.cut_count)
    print("=" * 62)
    print("  %d/%d files survived deliberately severed uploads" % (ok, len(files)))
    print("=" * 62)
    if server:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(root, ignore_errors=True)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())