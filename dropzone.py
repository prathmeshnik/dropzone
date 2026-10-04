#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dropzone.py -- a single-file, zero-dependency resumable file upload server,
designed to sit behind a Cloudflare Tunnel.

    python3 dropzone.py --key "some-long-secret" --port 9080
    cloudflared tunnel --url http://127.0.0.1:9080

Everything is stdlib. No pip install. Python 3.9+.
See README.md for the full API reference, deployment notes and limitations.

Why this exists instead of `python -m http.server`: public file upload over
the internet has three problems that a naive implementation gets wrong.

  1. CLOUDFLARE 100 MB REQUEST LIMIT.
     Cloudflare will reject *any single proxied request* whose body is larger
     than 100 MB with HTTP 413. A plain <form> POST of a 2 GB video therefore
     fails, and it fails late, after the user has already picked the file.
     -> This server speaks a resumable chunked protocol. Every individual
        request is tiny (default 1 MiB), so the limit is never hit, and an
        interrupted upload resumes instead of starting over.

  2. NO AUTHENTICATION.
     The moment a tunnel gives you a public hostname, anyone on the internet
     can reach it. -> Optional shared-secret key (header / cookie / query),
     constant-time compare, and per-IP rate limiting. It refuses to start
     unsecured unless you explicitly tell it to.

  3. REAL CLIENT IP.
     Behind a tunnel, socket peer is always 127.0.0.1, so per-IP limits and
     logs would be useless. -> Trusts CF-Connecting-IP / X-Forwarded-For,
     which is safe *because* the origin is not reachable directly.

------------------------------------------------------------------------
THE WEB UI (./web)
------------------------------------------------------------------------
Three static files sit next to this script and are the only ones the server
will read from disk:

    web/index.html   markup; __CONFIG__ is substituted per request
    web/style.css    the whole design system
    web/app.js       chunked upload protocol, resume, retries

The page is deliberately upload-first: the drop zone is the first and largest
thing on screen and everything else is subordinate to it. Underneath it:

  * status chips     limits and mode, read from /api/config, so they describe
                     this deployment rather than generic defaults
  * live strip       aggregate transfer speed, storage used, file count and
                     uptime, polled from /api/stats once a second while
                     anything is moving, every 4s when idle
  * queue            uploads started in THIS browser -- actionable:
                     pause, cancel, retry, copy link
  * incoming         uploads from somewhere ELSE (other people or devices).
                     Observational: no controls. The two lists are kept
                     disjoint on purpose: one page serves two audiences -- the
                     operator and whoever was sent the link -- and a session
                     showing up in both would read as a bug
  * files on server  recent uploads from /api/list, click to open
  * how it works     four steps, one line each
  * details          limits table, endpoint reference, known constraints,
                     all collapsed so they cannot bury the drop zone

There is no build step and no bundler: edit the file, reload the page.

------------------------------------------------------------------------
ENDPOINTS
------------------------------------------------------------------------
  GET  /                     Web UI (drag & drop, resume, progress)
  GET  /api/config           Limits + auth state, for the UI to read
  POST /api/auth             Exchange the key for an HttpOnly cookie
  POST /api/init             JSON  -> start a chunked upload, get an id
  PUT  /api/chunk/<id>/<n>   RAW   -> store one chunk (this is the hot path)
  POST /api/finish           JSON  -> concatenate + verify + move into place
  DELETE /api/chunk/<id>     Drop a partial upload
  POST /api/upload           multipart/form-data -> simple small-file path
  GET  /api/list             JSON list of stored files
  GET  /api/transfers        Progress of every upload in flight
  GET  /api/stats            Live dashboard: storage, speed, uptime, transfers
  GET  /d/<stored name>      Download (supports Range -> video scrubbing)
  GET  /healthz              Unauthenticated health probe

  /api/* and /d/* require the key when one is configured.
  /healthz never does.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import string
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from stat import S_ISREG
from email.parser import BytesParser
from email.policy import default as _default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIB = 1024 * 1024
READ_SIZE = 256 * 1024
API_VERSION = 1
MAX_JSON_BODY = 256 * 1024

# Cloudflare's documented proxied request body ceiling.
CLOUDFLARE_BODY_LIMIT = 100 * MIB

# Cloudflare also enforces an ~100s timeout on the whole proxied request,
# including the time the origin spends receiving the body. That limit, not the
# body-size ceiling, is what actually constrains us on slow or high-latency
# links (a phone over mobile data reaching a distant edge).
#
# Measured reality: a 1 MiB chunk can take 2-10s round trip to a far-away
# Cloudflare edge. An 8 MiB chunk with 3 parallel lanes then exceeds 100s and
# Cloudflare cancels mid-body -- which the origin sees as a truncated upload.
# Hence a conservative default: many small, short requests instead of a few
# huge, long ones.
CLOUDFLARE_TIMEOUT = 100

DEFAULT_CHUNK_SIZE = 1 * MIB
MIN_CHUNK_SIZE = 128 * 1024
MAX_CHUNK_SIZE = 16 * MIB  # well under the Cloudflare body ceiling

# Hard ceiling on how long we will wait on a single socket. Bounds thread
# occupancy when a client stalls mid-body.
SOCKET_TIMEOUT = 300

# How long a half-finished chunked upload may sit idle before reaping.
SESSION_TTL = 6 * 3600
MAX_SESSIONS = 500

# Storage/URL filename allowlist. Written as a *blacklist* of characters rather
# than a whitelist of characters, because the set of characters real clients
# legitimately send is effectively unbounded: non-ASCII ("IMG_0421é.jpg"),
# spaces ("My Video.mp4"), parentheses ("Photos (final).tar.gz"), emoji. A
# whitelist kept 404-ing on ordinary files.
#
# Rejected: control characters, path separators, and the characters that are
# hostile to filesystems and URLs (\ : * ? " < > |). The leading-dot ban keeps
# dotfiles (.parts, .git, .env) out of the browsable namespace. resolve() adds a
# second, independent check via realpath containment.
SAFE_NAME_RE = re.compile(r"^(?!\.)[^\x00-\x1f\x7f/\\:*?\"<>|]{1,190}$", re.UNICODE)
STATIC = {".html", ".css", ".js", ".json", ".txt", ".ico", ".svg", ".png"}

# Shown in the page footer / docs. Replace with the real repository URL before
# publishing; it is a single constant precisely so there is one place to fix.
REPO_URL = "https://github.com/prathmeshnik/dropzone"

# Where the web UI lives. index.html / style.css / app.js sit next to this
# script; they are the only files the server will ever read from disk.
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def human_bytes(n: float) -> str:
    if n < 1024:
        return "%d B" % n
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024.0
        if n < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit)
    return "%.1f TiB" % n


def now() -> float:
    return time.monotonic()


def log(fmt: str, *args) -> None:
    msg = fmt % args if args else fmt
    sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


def client_ip(handler: "Handler") -> str:
    """Real client IP, even behind a Cloudflare Tunnel.

    Safe to trust: the tunnel is the only thing that can reach the origin,
    and it overwrites these headers. If you ever expose the port directly to
    the internet, pass --trust-proxy off and rate limiting becomes spoofable.
    """
    host = handler.headers.get("CF-Connecting-IP", "").strip()
    if host:
        return host
    xff = handler.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    real = handler.headers.get("X-Real-IP", "").strip()
    if real:
        return real
    peer = handler.client_address[0] if handler.client_address else "-"
    return peer


def token_from_cookie(headers, name: str = "dz_key") -> str:
    raw = headers.get("Cookie", "")
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return urllib.parse.unquote(part[len(name) + 1 :])
    return ""


def make_slug(rng, n: int = 16) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(rng.choice(alphabet) for _ in range(n))


# ---------------------------------------------------------------------------
# Rate limiting (token bucket, keyed by client IP)
# ---------------------------------------------------------------------------


class RateLimiter:
    def __init__(self, rate_per_min: int, burst: int):
        self.rate = float(rate_per_min) / 60.0
        self.burst = float(burst)
        self._buckets: "OrderedDict[str, list]" = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        ts = now()
        with self._lock:
            b = self._buckets.get(ip)
            if b is None:
                b = [self.burst, ts]
                self._buckets[ip] = b
            else:
                self._buckets.move_to_end(ip)
            elapsed = ts - b[1]
            if elapsed > 0:
                b[0] = min(self.burst, b[0] + elapsed * self.rate)
                b[1] = ts
            if b[0] < 1.0:
                return False
            b[0] -= 1.0
            if len(self._buckets) > 20000:
                self._buckets.popitem(last=False)
            return True


# ---------------------------------------------------------------------------
# Upload sessions
# ---------------------------------------------------------------------------


class Session:
    """A resumable, chunked upload in progress.

    Chunks land in .part-<id>/ and are concatenated into the destination only
    once every chunk is present, so a partially-received file is never visible
    as a real download.
    """

    __slots__ = (
        "uid",
        "filename",
        "declared_size",
        "chunk_size",
        "total_chunks",
        "created",
        "touched",
        "received",
        "lock",
        "aborted",
    )

    def __init__(self, uid: str, filename: str, declared_size: int, chunk_size: int):
        self.uid = uid
        self.filename = filename
        self.declared_size = declared_size
        self.chunk_size = chunk_size
        self.total_chunks = (
            max(1, (declared_size + chunk_size - 1) // chunk_size) if declared_size > 0 else 0
        )
        self.created = now()
        self.touched = now()
        self.received = bytearray()  # parallel to range(); 1 == present
        self.lock = threading.Lock()
        self.aborted = False

    def expect_chunks(self) -> int:
        if self.total_chunks:
            return self.total_chunks
        # Unknown size (client streaming without a length): accept anything
        # reasonable; the finish step is what actually enforces the limit.
        return 1 << 20

    def mark(self, index: int, nbytes: int) -> None:
        with self.lock:
            self.touched = now()
            while len(self.received) <= index:
                self.received.append(0)
            self.received[index] = 1
        del nbytes

    def has(self, index: int) -> bool:
        with self.lock:
            return index < len(self.received) and self.received[index] == 1

    def missing(self) -> list:
        """Indices the client still owes us, over the full declared range.

        Must iterate to total_chunks, not just len(received): the bytearray
        only grows as far as the highest index actually received, so a client
        that sends chunk 0 of 10 would otherwise look complete.
        """
        with self.lock:
            total = self.total_chunks or len(self.received)
            return [i for i in range(total) if i >= len(self.received) or self.received[i] != 1]

    def bytes_so_far(self) -> int:
        with self.lock:
            if not self.total_chunks:
                return sum(self.received)
            total = 0
            for i in range(min(self.total_chunks, len(self.received))):
                if self.received[i] == 1:
                    start = i * self.chunk_size
                    total += min(self.chunk_size, self.declared_size - start)
            return total

    def snapshot(self) -> dict:
        """Progress view for the server owner's activity feed.

        Deliberately excludes the client IP. This endpoint sits behind the
        same shared key as everything else, and that key is routinely handed
        to more than one person; the operator needs to see *that* a file is
        arriving, not who it came from.

        NOTE: self.lock is a plain Lock, not an RLock, so this must not call
        bytes_so_far() or missing() while holding it -- either would deadlock
        the request thread and, with it, every other upload. The byte total
        is therefore recomputed inline here.
        """
        with self.lock:
            span = self.total_chunks or len(self.received)
            received = 0
            stored = 0
            for i in range(min(span, len(self.received))):
                if self.received[i] == 1:
                    received += 1
                    if self.total_chunks:
                        start = i * self.chunk_size
                        stored += min(self.chunk_size, self.declared_size - start)
                    else:
                        stored += 1
            t = now()
            return {
                "id": self.uid,
                "name": self.filename,
                "size": self.declared_size,
                "chunkSize": self.chunk_size,
                "totalChunks": self.total_chunks,
                "receivedChunks": received,
                "bytes": stored,
                "elapsed": max(0.0, t - self.created),
                "idle": max(0.0, t - self.touched),
            }


class SessionStore:
    def __init__(self, root: str, max_file_size: int, part_root: str | None = None):
        self.root = root
        self.max_file_size = max_file_size
        # Partials live outside the browsable directory so a half-uploaded file
        # is never visible or downloadable.
        self.part_root = os.path.abspath(part_root or os.path.join(root, ".parts"))
        os.makedirs(self.part_root, exist_ok=True)
        self._sessions: "OrderedDict[str, Session]" = OrderedDict()
        self._lock = threading.Lock()

    def part_dir(self, uid: str) -> str:
        safe = re.sub(r"[^a-z0-9]", "", uid)
        if len(safe) < 8:
            raise ValueError("bad upload id")
        return os.path.join(self.part_root, "up-" + safe)

    def create(self, filename: str, size: int, chunk_size: int) -> Session:
        if self.max_file_size and size > self.max_file_size:
            raise ValueError(
                "file exceeds the configured limit (%s)" % human_bytes(self.max_file_size)
            )
        if chunk_size < MIN_CHUNK_SIZE or chunk_size > MAX_CHUNK_SIZE:
            chunk_size = DEFAULT_CHUNK_SIZE
        uid = make_slug(secrets.SystemRandom(), 24)
        os.makedirs(self.part_dir(uid), exist_ok=True)
        sess = Session(uid, filename, size, chunk_size)
        with self._lock:
            self._sessions[uid] = sess
            self._sessions.move_to_end(uid)
            while len(self._sessions) > MAX_SESSIONS:
                _, victim = self._sessions.popitem(last=False)
                self._nuke(victim)
        return sess

    def get(self, uid: str) -> "Session | None":
        with self._lock:
            s = self._sessions.get(uid)
            if s is not None:
                self._sessions.move_to_end(uid)
            return s

    def drop(self, uid: str) -> None:
        with self._lock:
            s = self._sessions.pop(uid, None)
        if s is not None:
            self._nuke(s)

    def _nuke(self, s: Session) -> None:
        shutil_rmtree(self.part_dir(s.uid))

    def reap(self) -> int:
        """Delete abandoned sessions older than SESSION_TTL."""
        cutoff = now() - SESSION_TTL
        dead = []
        with self._lock:
            for uid, s in list(self._sessions.items()):
                if s.touched < cutoff:
                    dead.append(uid)
            for uid in dead:
                self._sessions.pop(uid, None)
        for uid in dead:
            try:
                shutil_rmtree(self.part_dir(uid))
            except OSError:
                pass
            log("reaped stale upload session %s", uid[:8])
        return len(dead)

    def active_bytes(self) -> int:
        total = 0
        with self._lock:
            for s in self._sessions.values():
                total += s.bytes_so_far()
        return total

    def active(self) -> list:
        """Progress snapshots of every upload currently in flight.

        Newest first. Read under the store lock so a concurrent reap() or
        drop() cannot mutate the dict mid-iteration, then snapshot each
        session outside it -- snapshot() takes the per-session lock, and
        holding both in a fixed order here would be a lock-ordering hazard.
        """
        with self._lock:
            sessions = list(self._sessions.values())
        out = [s.snapshot() for s in sessions]
        # Newest first. `elapsed` is the age of the session, so the largest
        # elapsed is the oldest.
        out.sort(key=lambda d: d["elapsed"], reverse=True)
        return out


def shutil_rmtree(path: str) -> None:
    try:
        entries = os.listdir(path)
    except OSError:
        return
    for name in entries:
        full = os.path.join(path, name)
        try:
            if os.path.islink(full) or os.path.isfile(full):
                os.unlink(full)
            else:
                shutil_rmtree(full)
        except OSError:
            pass
    try:
        os.rmdir(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


# Compound extensions that must stay glued together when we de-duplicate a
# filename. Splitting "photos.tar.gz" into "photos.tar" + ".gz" and then
# appending " (2)" produces "photos.tar (2).gz", which no longer looks like a
# gzip tarball to the OS or to whoever downloads it.
COMPOUND_EXT = (
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz4",
    ".user.js", ".min.js", ".min.css", ".d.ts", ".tar.z",
)


def split_ext(name: str) -> "tuple[str, str]":
    """Split into (stem, extension) keeping compound extensions intact."""
    low = name.lower()
    for ext in COMPOUND_EXT:
        if low.endswith(ext) and len(name) > len(ext):
            return name[: -len(ext)], name[-len(ext):]
    stem, ext = os.path.splitext(name)
    return stem, ext


class Storage:
    def __init__(self, root: str, subdir: str | None, serve: bool):
        self.root = os.path.abspath(root)
        self.subdir = subdir
        self.serve = serve
        if subdir:
            self.dir = os.path.abspath(os.path.join(root, subdir))
        else:
            self.dir = self.root
        os.makedirs(self.dir, exist_ok=True)
        self.part_root = os.path.join(self.dir, ".parts")
        os.makedirs(self.part_root, exist_ok=True)
        self._lock = threading.Lock()

    def _is_transient(self, name: str) -> bool:
        """True for files that are about to be renamed into a real file.

        During assemble() the destination exists twice: once as the loose
        chunks in .parts and once as the ".assembling-*" file being written.
        Counting both makes a directory look like it holds twice the data it
        does, which spuriously trips --max-files and the 507 guard. The bytes
        are real but transient, so they must not count toward the quota.
        """
        return name.startswith(".") or ".assembling-" in name

    def _scan_bytes(self) -> int:
        """Bytes stored. Caller must already hold self._lock.

        Split out from allocated() because self._lock is a plain Lock, not an
        RLock: assemble() needs to measure usage *while holding* that lock to
        keep concurrent finishes from jointly overshooting the quota, and
        calling the locking accessor from in there would deadlock the thread.
        """
        total = 0
        for base in (self.dir, self.part_root):
            try:
                names = os.listdir(base)
            except OSError:
                continue
            for name in names:
                if self._is_transient(name):
                    continue
                full = os.path.join(base, name)
                try:
                    if os.path.isfile(full):
                        total += os.stat(full).st_size
                except OSError:
                    continue
        for name in os.listdir(self.part_root) if os.path.isdir(self.part_root) else []:
            sub = os.path.join(self.part_root, name)
            try:
                for inner in os.listdir(sub):
                    if self._is_transient(inner):
                        continue
                    f = os.path.join(sub, inner)
                    if os.path.isfile(f):
                        total += os.stat(f).st_size
            except OSError:
                continue
        return total

    def allocated(self) -> int:
        """Bytes stored. Includes in-flight chunks so the quota reflects
        what the disk is actually consuming, not just finished files."""
        with self._lock:
            return self._scan_bytes()

    def unique_path(self, filename: str) -> str:
        """Never clobber: append ' (2)', ' (3)', ... on collision."""
        base = os.path.join(self.dir, filename)
        if not os.path.exists(base):
            return base
        stem, ext = split_ext(filename)
        for n in range(2, 10000):
            cand = os.path.join(self.dir, "%s (%d)%s" % (stem, n, ext))
            if not os.path.exists(cand):
                return cand
        return os.path.join(self.dir, stem + "-" + make_slug(secrets.SystemRandom(), 8) + ext)

    def resolve(self, stored: str) -> "str | None":
        """Map a URL name to a real path, refusing traversal."""
        if not stored:
            return None
        try:
            decoded = urllib.parse.unquote(stored)
        except Exception:
            return None
        if "/" in decoded or "\\" in decoded or "\x00" in decoded:
            return None
        if decoded in (".", "..") or decoded.startswith("."):
            return None
        if not SAFE_NAME_RE.match(decoded):
            return None
        full = os.path.abspath(os.path.join(self.dir, decoded))
        if os.path.dirname(full) != os.path.abspath(self.dir):
            return None
        if not os.path.isfile(full):
            return None
        return full

    def storable(self, name: str) -> bool:
        """True if a name we just wrote can also be read back."""
        return bool(name) and not name.startswith(".") and bool(SAFE_NAME_RE.match(name))

    def accounting(self) -> dict:
        """One walk of the tree: finished bytes/files plus in-flight partials.

        Separate from allocated() because the dashboard needs the file count
        and the partial total as well, and paying for a second directory walk
        to get them would double the cost of every poll.
        """
        total = 0
        files = 0
        partial = 0
        with self._lock:
            try:
                entries = os.listdir(self.dir)
            except OSError:
                entries = []
            for name in entries:
                if self._is_transient(name):
                    continue
                full = os.path.join(self.dir, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if not S_ISREG(st.st_mode):
                    continue
                total += st.st_size
                files += 1
            # .parts/<session>/<chunk> -- count the bytes so the dashboard can
            # show what is still on its way to disk.
            try:
                sessions = os.listdir(self.part_root)
            except OSError:
                sessions = []
            for sname in sessions:
                sub = os.path.join(self.part_root, sname)
                try:
                    inner = os.listdir(sub)
                except OSError:
                    continue
                for iname in inner:
                    if self._is_transient(iname):
                        continue
                    try:
                        st = os.stat(os.path.join(sub, iname))
                    except OSError:
                        continue
                    if S_ISREG(st.st_mode):
                        partial += st.st_size
        return {"bytes": total, "files": files, "partialBytes": partial}

    def list(self, limit: int = 500) -> list:
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for name in names:
            if name.startswith("."):
                continue
            if not SAFE_NAME_RE.match(name):
                continue
            full = os.path.join(self.dir, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not os.path.isfile(full):
                continue
            out.append(
                {
                    "name": name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "url": "/d/" + urllib.parse.quote(name),
                }
            )
        out.sort(key=lambda d: d["mtime"], reverse=True)
        return out[:limit]


# ---------------------------------------------------------------------------
# Multipart parsing (hand-rolled: `cgi` is deprecated/removed in 3.13, and a
# streaming parse lets us handle 10 GB bodies without buffering them in RAM)
# ---------------------------------------------------------------------------


class _ChunkedReader:
    def __init__(self, fh, bufsize: int = READ_SIZE):
        self.fh = fh
        self.buf = b""
        self.base = 0  # file offset of buf[0]
        self.eof = False
        self.bufsize = bufsize

    def _refill(self) -> None:
        if self.eof:
            return
        data = self.fh.read(self.bufsize)
        if not data:
            self.eof = True
        else:
            self.buf += data

    def find(self, needle: bytes) -> int:
        """Buffer-relative index of needle, or -1 at EOF."""
        while True:
            i = self.buf.find(needle)
            if i != -1:
                return i
            if self.eof:
                return -1
            keep = len(needle) - 1
            if len(self.buf) > keep:
                drop = len(self.buf) - keep
                self.base += drop
                self.buf = self.buf[keep:]
            self._refill()

    def discard(self, n: int) -> None:
        self.base += n
        self.buf = self.buf[n:]

    def at(self, n: int) -> bytes:
        while len(self.buf) < n and not self.eof:
            self._refill()
        return self.buf[:n]

    def take_until(self, needle: bytes) -> "bytes | None":
        i = self.find(needle)
        if i == -1:
            return None
        out = self.buf[:i]
        self.discard(i + len(needle))
        return out

    def take(self, n: int) -> bytes:
        out = self.at(n)
        self.discard(len(out))
        return out


class PartReader:
    """Yields (headers, body_start, body_end) offsets inside a spool file."""

    def __init__(self, fh, boundary: bytes):
        self.r = _ChunkedReader(fh)
        self.boundary = boundary

    def __iter__(self):
        delim = b"--" + self.boundary
        i = self.r.find(delim)
        if i == -1:
            return
        self.r.discard(i + len(delim))
        while True:
            peek = self.r.at(2)
            if peek == b"--":
                return
            if peek[:1] != b"\r":
                return
            self.r.discard(2)
            raw = self.r.take_until(b"\r\n\r\n")
            if raw is None:
                return
            hdrs = BytesParser(policy=_default_policy).parsebytes(raw + b"\r\n\r\n")
            body_start = self.r.base
            j = self.r.find(b"\r\n" + delim)
            if j == -1:
                return
            body_end = self.r.base + j
            yield hdrs, body_start, body_end
            self.r.discard(j + 2 + len(delim))


def copy_range(src_path: str, start: int, end: int, dst_fh, hashers=()) -> int:
    written = 0
    with open(src_path, "rb") as f:
        f.seek(start)
        remaining = end - start
        while remaining > 0:
            chunk = f.read(min(READ_SIZE, remaining))
            if not chunk:
                break
            dst_fh.write(chunk)
            for h in hashers:
                h.update(chunk)
            written += len(chunk)
            remaining -= len(chunk)
    return written


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Config:
    def __init__(self, args):
        self.key = args.key
        self.upload_dir = os.path.abspath(args.dir)
        self.max_file_size = args.max_file_size
        self.max_files = args.max_files
        self.chunked = args.chunked
        self.chunk_size = args.chunk_size
        self.parallel = args.parallel
        self.serve = args.serve
        self.insecure = args.insecure
        self.trust_proxy = args.trust_proxy
        self.hosts = [h.strip().lower() for h in args.hosts.split(",") if h.strip()]
        self.storage = Storage(self.upload_dir, args.subdir, args.serve)
        self.sessions = SessionStore(self.upload_dir, args.max_file_size, self.storage.part_root)
        self.limiter = RateLimiter(args.rate_limit, args.rate_burst)
        # The data plane (PUT /api/chunk/...) needs a much larger budget than
        # the control plane. A 1 GB file at the default 8 MiB chunk size is
        # 128 requests by itself, so sharing one budget would break large
        # uploads mid-transfer with a spurious 429.
        self.chunk_limiter = RateLimiter(
            args.chunk_rate_limit or args.rate_limit * 12, args.chunk_rate_burst or 600)
        self.web_dir = WEB_DIR
        self.started = time.time()
        # Short-TTL cache for the dashboard. Storage accounting is a directory
        # walk (~16 ms at 5k files, growing linearly), and several viewers may
        # poll at once; recomputing per request would turn a read-only
        # dashboard into measurable disk load.
        self._stats_cache = None
        self._stats_at = 0.0
        self._stats_lock = threading.Lock()

    def stats(self, max_age: float = 1.0) -> dict:
        """Snapshot for the dashboard, recomputed at most once per `max_age`."""
        t = time.time()
        with self._stats_lock:
            if self._stats_cache is not None and (t - self._stats_at) < max_age:
                return self._stats_cache
        acct = self.storage.accounting()
        transfers = self.sessions.active()
        used = acct["bytes"] + acct["partialBytes"]
        # Each transfer reports its own elapsed time; the aggregate is the sum
        # of bytes over the sum of elapsed, which is the correct way to average
        # rates across transfers that started at different moments.
        elapsed = sum(x["elapsed"] for x in transfers) or 0.0
        speed = 0.0
        if elapsed > 0.5:
            speed = sum(x["bytes"] for x in transfers) / elapsed
        data = {
            "version": API_VERSION,
            "uptime": max(0.0, t - self.started),
            "storage": {
                "bytes": used,
                "files": acct["files"],
                "partialBytes": acct["partialBytes"],
                "quota": self.max_files,
                "pct": (used / self.max_files * 100.0) if self.max_files else None,
            },
            "transfers": transfers,
            "speed": speed,
            "activeCount": len(transfers),
        }
        with self._stats_lock:
            self._stats_cache = data
            self._stats_at = time.time()
        return data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dropzone"
    sys_version = ""
    # Bound how long a single request may occupy a thread. Without this, a
    # stalled client mid-body pins a thread forever.
    timeout = SOCKET_TIMEOUT

    # -- plumbing ----------------------------------------------------------

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))

    def client_ip(self) -> str:
        if not self.cfg.trust_proxy:
            return self.client_address[0] if self.client_address else "-"
        return client_ip(self)

    # -- responses ---------------------------------------------------------

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              extra: dict | None = None) -> None:
        # A client that vanished mid-upload (Cloudflare cancelling a timed-out
        # request) is routine, not an error. Writing to it raises BrokenPipe,
        # and letting that escape produced a stack trace per dead chunk.
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            # Connection is unusable; stop reusing it rather than trying to
            # parse whatever is left in it as a new request.
            self.close_connection = True

    def json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def drain_body(self, limit: int = 8 * MIB) -> None:
        """Discard an unread request body so keep-alive stays in sync.

        This matters specifically because cloudflared pools HTTP/1.1
        connections to the origin. If we answer early (404, 413, 429...) and
        leave an 8 MiB body in the socket, the next request on that pooled
        connection is parsed out of the leftover body bytes: garbage, then a
        close, which cloudflared reports as "use of closed network
        connection". Either drain it or retire the connection.
        """
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            return
        if n <= 0:
            return
        if n > limit:
            # Too big to waste time on; make sure it cannot be reused.
            self.close_connection = True
            return
        remaining = n
        try:
            while remaining > 0:
                data = self.rfile.read(min(READ_SIZE, remaining))
                if not data:
                    self.close_connection = True
                    return
                remaining -= len(data)
        except (OSError, ValueError):
            self.close_connection = True

    def err(self, code: int, msg: str) -> None:
        self.json({"error": msg}, code)

    def host_ok(self) -> bool:
        if not self.cfg.hosts:
            return True
        host = self.headers.get("Host", "").split(":")[0].lower()
        return host in self.cfg.hosts

    def authorized(self) -> bool:
        key = self.cfg.key
        if not key:
            return True
        cands = [
            self.headers.get("X-Upload-Key", ""),
            token_from_cookie(self.headers),
        ]
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            cands.append(auth[7:].strip())
        elif auth.startswith("Basic "):
            try:
                raw = base64.b64decode(auth[6:].strip()).decode("utf-8", "replace")
                cands.append(raw.split(":", 1)[1] if ":" in raw else raw)
            except Exception:
                pass
        q = urllib.parse.urlparse(self.path).query
        kv = urllib.parse.parse_qs(q)
        cands.extend(kv.get("k", []))
        for c in cands:
            if c and hmac.compare_digest(c, key):
                return True
        return False

    def guard(self, api: bool = True, limiter=None) -> bool:
        # Every rejection below answers without reading the body, so drain
        # first -- otherwise a pooled keep-alive connection gets poisoned.
        if not self.host_ok():
            self.drain_body()
            self.err(421, "unknown host")
            return False
        if not self.authorized():
            self.drain_body()
            if api:
                self.err(401, "invalid or missing key")
            else:
                self._send(401, b"invalid or missing key\n")
            return False
        if api:
            lim = limiter if limiter is not None else self.cfg.limiter
            if lim is not None and not lim.allow(self.client_ip()):
                self.drain_body()
                self.err(429, "too many requests")
                return False
            if self.cfg.max_files and self.cfg.storage.allocated() > self.cfg.max_files:
                self.drain_body()
                self.err(507, "server storage limit reached")
                return False
        return True

    def read_body(self, limit: int) -> bytes:
        """Read the request body into memory, enforcing `limit`."""
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        if n > limit:
            # Must drain: bailing out here would leave the body in the socket
            # and desync the next request on a pooled keep-alive connection.
            self.drain_body()
            raise ValueError("body too large")
        out = bytearray()
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(READ_SIZE, remaining))
            if not chunk:
                break
            out.extend(chunk)
            remaining -= len(chunk)
        return bytes(out)

    def read_json(self) -> dict:
        raw = self.read_body(MAX_JSON_BODY)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", "replace"))

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        p = urllib.parse.urlsplit(self.path).path
        if p == "/healthz":
            self._send(200, b"ok", extra={"Cache-Control": "no-store"})
            return
        if p in ("/", "/index.html"):
            # The page itself is served unauthenticated: it holds no secrets,
            # and the browser has to be able to load it in order to render the
            # key prompt. Everything it can *do* still goes through guard().
            self.serve_index()
            return
        if p in self.WEB_FILES and p not in ("/", "/index.html"):
            self.serve_web(p)
            return
        if p == "/favicon.ico":
            # Browsers request this unprompted. Answer with the real icon
            # rather than a 404, which otherwise shows up as a broken request
            # on every single page load.
            self.serve_web("/icon.png")
            return
        if p == "/api/config":
            if not self.guard():
                return
            self.json(self.client_config())
            return
        if p == "/api/list":
            if not self.guard():
                return
            self.json({"files": self.cfg.storage.list()})
            return
        if p == "/api/transfers":
            # Live progress of every upload in flight. This is the "receiver"
            # half of the UI: whoever runs the server opens the same page and
            # watches files arrive, without having to tail the log.
            if not self.guard():
                return
            self.json({"transfers": self.cfg.sessions.active()})
            return
        if p == "/api/stats":
            # Everything the dashboard shows, in one round trip. Deliberately
            # a single endpoint: polling four separate URLs every second
            # would quadruple the request budget for no benefit, and the
            # numbers are meaningless if they arrive a few hundred ms apart.
            if not self.guard():
                return
            self.json(self.cfg.stats())
            return
        if p.startswith("/api/chunk/"):
            # GET /api/chunk/<id> -> resume probe: which chunks do you have?
            if not self.guard():
                return
            return self.handle_status(p[len("/api/chunk/"):])
        if p.startswith("/d/"):
            if not self.guard(api=False):
                return
            self.serve_download(urllib.parse.unquote(p[3:]))
            return
        self.err(404, "not found")

    def do_POST(self) -> None:
        p = urllib.parse.urlsplit(self.path).path
        if p == "/api/auth":
            self.handle_auth()
            return
        if p == "/api/init":
            if not self.guard():
                return
            self.handle_init()
            return
        if p == "/api/finish":
            if not self.guard():
                return
            self.handle_finish()
            return
        if p == "/api/upload":
            if not self.guard():
                return
            self.handle_simple()
            return
        self.err(404, "not found")

    def do_PUT(self) -> None:
        p = urllib.parse.urlsplit(self.path).path
        if p.startswith("/api/chunk/"):
            # Chunk PUTs draw from the (much larger) data-plane budget.
            if not self.guard(limiter=self.cfg.chunk_limiter):
                return
            rest = p[len("/api/chunk/"):]
            if "/" in rest:
                uid, _, idx = rest.rpartition("/")
                if idx.isdigit():
                    return self.handle_chunk(uid, int(idx))
            self.drain_body()
            self.err(400, "bad chunk path")
            return
        self.err(404, "not found")

    def do_HEAD(self) -> None:  # noqa: F811  (routing alias)
        self.do_GET()

    def handle_status(self, uid: str) -> None:
        """Ask the server which chunks it still needs. Used on resume."""
        sess = self.cfg.sessions.get(uid)
        if sess is None:
            return self.err(404, "unknown upload id")
        missing = sess.missing()
        self.json({
            "uploadId": uid,
            "chunkSize": sess.chunk_size,
            "totalChunks": sess.total_chunks,
            "received": [i for i in range(sess.total_chunks) if i not in missing] if sess.total_chunks else [],
            "missing": missing[:5000],
            "bytesStored": sess.bytes_so_far(),
        })

    def do_DELETE(self) -> None:
        p = urllib.parse.urlsplit(self.path).path
        if p.startswith("/api/chunk/"):
            if not self.guard():
                return
            self.cfg.sessions.drop(p[len("/api/chunk/"):])
            self.json({"ok": True})
            return
        self.err(404, "not found")

    # -- handlers ----------------------------------------------------------

    def client_config(self) -> dict:
        return {
            "version": API_VERSION,
            "authRequired": bool(self.cfg.key),
            "chunked": self.cfg.chunked,
            "chunkSize": self.cfg.chunk_size,
            # Kept low deliberately. On a bandwidth-constrained link each
            # parallel lane splits the same pipe, so every individual request
            # takes proportionally longer and is more likely to hit
            # Cloudflare's per-request timeout. 2 lanes hide RTT without
            # inflating request duration.
            "parallel": max(1, min(6, self.cfg.parallel)),
            "requestTimeout": int(CLOUDFLARE_TIMEOUT * 1000 * 0.9),
            "maxFileSize": self.cfg.max_file_size,
            "maxTotal": self.cfg.max_files,
            "resumeBelow": 256 * MIB,
            "uploaded": self.cfg.storage.allocated(),
            "cloudflareBodyLimit": CLOUDFLARE_BODY_LIMIT,
            # The UI shows a "files on this server" panel and a limits table.
            # Both are pointless when downloads are switched off.
            "serveDownloads": bool(self.cfg.serve),
            "repo": REPO_URL,
        }

    # -- static web assets --------------------------------------------------
    #
    # The UI lives in ./web (index.html, style.css, app.js) rather than inside
    # this file. Only these three names are ever served, and each is resolved
    # against a fixed map, so there is no user-controlled path to sanitise and
    # no way to walk out of the directory.

    WEB_FILES = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/style.css": ("style.css", "text/css; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/og.png": ("og.png", "image/png"),
        "/icon.png": ("icon.png", "image/png"),
    }

    def absolute_url(self, path: str) -> str:
        """Build an absolute URL from the request's own Host and scheme.

        Open Graph requires absolute image/canonical URLs, and there is no
        configured public hostname to hardcode -- dropzone is meant to run
        behind whatever tunnel or reverse proxy you point at it, so the only
        authority that knows the right answer is the incoming request.

        The Host header is client-controlled and this value is interpolated
        into an HTML attribute, so it is filtered down to characters that
        cannot terminate the attribute or smuggle markup.
        """
        host = re.sub(r"[^A-Za-z0-9.:\[\]-]", "", self.headers.get("Host", "").strip())
        if not host or len(host) > 255:
            return ""
        return "%s://%s%s" % ("https" if self.is_https() else "http", host, path)

    def serve_web(self, path: str) -> None:
        entry = self.WEB_FILES.get(path)
        if entry is None:
            self.err(404, "not found")
            return
        name, ctype = entry
        try:
            with open(os.path.join(self.cfg.web_dir, name), "rb") as f:
                raw = f.read()
        except OSError:
            self.err(500, "web assets missing (expected a ./web/ directory next to dropzone.py)")
            return
        if name == "index.html":
            # __CONFIG__ is substituted per request because it carries this
            # deployment's limits and auth state. "</" is escaped so the JSON
            # blob cannot terminate the surrounding <script> element.
            cfg = json.dumps(self.client_config()).replace("</", "<\\/")
            raw = raw.replace(b"__CONFIG__", cfg.encode())
            # Open Graph needs absolute URLs. If the Host header is missing or
            # nonsensical there is nothing truthful to emit, so the tags end up
            # empty and the unfurler falls back to showing text only.
            base = self.absolute_url("")
            raw = raw.replace(b"__OG_URL__", html.escape(base + "/", quote=True).encode())
            raw = raw.replace(b"__OG_IMAGE__", html.escape(base + "/og.png", quote=True).encode())
            cache = "no-cache"
        else:
            # Assets carry no per-deployment state, so let the browser keep
            # them. `no-cache` (not no-store) still revalidates, which is what
            # picks up a new deploy without a hard refresh.
            cache = "public, max-age=0, must-revalidate"
        self._send(200, raw, ctype, {"Cache-Control": cache})

    def serve_index(self) -> None:
        self.serve_web("/")

    def handle_auth(self) -> None:
        if not self.cfg.key:
            self.json({"ok": True, "authRequired": False})
            return
        try:
            data = self.read_json()
        except Exception:
            self.err(400, "bad json")
            return
        supplied = str(data.get("key", ""))
        if not supplied or not hmac.compare_digest(supplied, self.cfg.key):
            self.err(401, "incorrect key")
            return
        cookie = (
            "dz_key=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s"
            % (
                urllib.parse.quote(self.cfg.key),
                12 * 3600,
                "; Secure" if self.is_https() else "",
            )
        )
        self._send(200, json.dumps({"ok": True}).encode(), "application/json",
                   {"Set-Cookie": cookie})

    def is_https(self) -> bool:
        if not self.cfg.trust_proxy:
            return False
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            return True
        return self.headers.get("Cf-Visitor", "").find("https") != -1

    def handle_init(self) -> None:
        if not self.cfg.chunked:
            # Non-chunked mode: hand back a one-shot instruction.
            self.json({"mode": "simple", "chunked": False})
            return
        try:
            data = self.read_json()
        except Exception:
            return self.err(400, "bad json")
        name = sanitize_filename(str(data.get("name", "")))
        try:
            size = int(data.get("size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        if size < 0:
            return self.err(400, "bad size")
        if self.cfg.max_file_size and size > self.cfg.max_file_size:
            return self.err(413, "file exceeds limit (%s)" % human_bytes(self.cfg.max_file_size))
        try:
            cs = int(data.get("chunkSize", self.cfg.chunk_size))
        except (TypeError, ValueError):
            cs = self.cfg.chunk_size
        try:
            sess = self.cfg.sessions.create(name, size, cs)
        except ValueError as e:
            return self.err(413, str(e))
        log("%s -> init %s (%s) %s", self.client_ip(), name, human_bytes(size), sess.uid[:8])
        self.json({
            "uploadId": sess.uid,
            "chunkSize": sess.chunk_size,
            "totalChunks": sess.total_chunks,
            "received": [],
        })

    def handle_chunk(self, uid: str, index: int) -> None:
        sess = self.cfg.sessions.get(uid)
        if sess is None:
            self.drain_body()
            return self.err(404, "unknown upload id (it may have expired - restart this file)")
        try:
            expect = sess.expect_chunks()
        except ValueError:
            self.drain_body()
            return self.err(400, "bad upload id")
        if index >= expect:
            self.drain_body()
            return self.err(400, "chunk index out of range")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_CHUNK_SIZE:
            self.drain_body()
            return self.err(413, "chunk too large (max %s)" % human_bytes(MAX_CHUNK_SIZE))
        path = os.path.join(self.cfg.sessions.part_dir(uid), "%08d" % index)
        tmp = path + ".tmp"
        written = 0
        try:
            with open(tmp, "wb") as f:
                remaining = length
                while remaining > 0:
                    try:
                        data = self.rfile.read(min(READ_SIZE, remaining))
                    except (socket.timeout, TimeoutError):
                        raise IOError("timed out reading body")
                    if not data:
                        raise IOError("short read: got %d of %d bytes" % (written, length))
                    f.write(data)
                    written += len(data)
                    remaining -= len(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except (OSError, IOError) as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            # A truncated body means the peer went away mid-transfer (almost
            # always Cloudflare cancelling a request that ran too long). It is
            # a client-side, retryable condition, not a server fault: answer
            # 408 so the client retries this chunk instead of failing the file.
            truncated = "short read" in str(e) or "timed out" in str(e)
            log("chunk %d/%s incomplete after %d/%d bytes: %s",
                index, uid[:8], written, length, e)
            self.close_connection = True
            code = 408 if truncated else 500
            return self.err(code, "incomplete chunk (%d of %d bytes); retry this chunk"
                            % (written, length))
        sess.mark(index, written)
        missing = sess.missing()
        self.json({
            "ok": True,
            "index": index,
            "received": sess.total_chunks - len(missing) if sess.total_chunks else len(sess.received),
            "totalChunks": sess.total_chunks,
            "missing": missing[:200],
            "remaining": len(missing),
        })

    def handle_finish(self) -> None:
        try:
            data = self.read_json()
        except Exception:
            return self.err(400, "bad json")
        uid = str(data.get("uploadId", ""))
        sess = self.cfg.sessions.get(uid)
        if sess is None:
            return self.err(404, "unknown upload id")
        missing = sess.missing()
        if sess.total_chunks and missing:
            return self.err(409, "incomplete upload: %d chunk(s) missing" % len(missing))

        part_dir = self.cfg.sessions.part_dir(uid)
        expect_bytes = int(data.get("size", sess.declared_size) or 0)
        try:
            result = self.assemble(sess, part_dir, expect_bytes)
        except Exception as e:
            log("assemble failed %s: %s", uid[:8], e)
            return self.err(500, "failed to assemble file: %s" % e)
        self.cfg.sessions.drop(uid)
        result["ok"] = True
        log("%s -> saved %s (%s)", self.client_ip(), result["name"], human_bytes(result["size"]))
        self.json(result)

    def assemble(self, sess: Session, part_dir: str, expect_bytes: int) -> dict:
        """Concatenate chunks into the destination, verifying size."""
        chunks = sorted(os.listdir(part_dir))
        chunks = [c for c in chunks if c.isdigit()]
        if not chunks:
            raise IOError("no chunks received")
        dest = self.cfg.storage.unique_path(sess.filename)
        if not self.cfg.storage.storable(os.path.basename(dest)):
            raise IOError("filename %r cannot be served back safely" % sess.filename)
        tmp = dest + ".assembling-%s" % make_slug(secrets.SystemRandom(), 6)
        sha = hashlib.sha256()
        total = 0
        try:
            with open(tmp, "wb") as out:
                for name in chunks:
                    p = os.path.join(part_dir, name)
                    total += copy_range(p, 0, os.path.getsize(p), out, [sha])
                out.flush()
                os.fsync(out.fileno())
            if expect_bytes and total != expect_bytes:
                raise IOError("size mismatch: got %d, expected %d" % (total, expect_bytes))
            if self.cfg.max_file_size and total > self.cfg.max_file_size:
                raise IOError("file exceeds limit")
            if self.cfg.max_files:
                # Re-check under a lock: concurrent finishes could each pass a
                # naive check and jointly overshoot the quota.
                with self.cfg.storage._lock:
                    # _scan_bytes, NOT allocated(): we already hold this
                    # lock and it is not reentrant.
                    #
                    # Deliberately NOT "+ total >". The chunks for this
                    # upload are already on disk and _scan_bytes() already
                    # counts them, and assembling relocates those bytes into
                    # the final file rather than adding any. Adding `total`
                    # again charged every file twice, so with a quota set any
                    # file larger than half the quota could never be saved.
                    #
                    # This is safe: the quota is enforced again on every
                    # request by guard(), so chunks that would push the
                    # directory past the limit are refused long before this
                    # point, and the assembled size has already been checked
                    # against expect_bytes and max_file_size above.
                    if self.cfg.storage._scan_bytes() > self.cfg.max_files:
                        raise IOError("server storage quota exceeded")
                    os.replace(tmp, dest)
            else:
                os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        digest = sha.hexdigest()
        st = os.stat(dest)
        return {
            "name": os.path.basename(dest),
            "size": st.st_size,
            "sha256": digest,
            "url": "/d/" + urllib.parse.quote(os.path.basename(dest)),
        }

    def handle_simple(self) -> None:
        """One-shot multipart upload, for API clients and small files."""
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if "multipart/form-data" not in ctype or not m:
            return self.err(400, "expected multipart/form-data")
        boundary = m.group(1).encode()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        # Cap the spool at whatever the limits allow. Streaming to disk (rather
        # than buffering in RAM) is what lets a 10 GB file work at all.
        cap = (self.cfg.max_file_size + 16 * MIB) if self.cfg.max_file_size else (1024 * MIB)
        if self.cfg.max_files:
            cap = min(cap, self.cfg.max_files + 16 * MIB)
        if length > cap:
            return self.err(413, "body too large for the simple endpoint; use the chunked endpoint")

        spool = os.path.join(self.cfg.sessions.part_root, "spool-" + make_slug(secrets.SystemRandom(), 12))
        results = []
        try:
            with open(spool, "wb") as f:
                remaining = length
                while remaining > 0:
                    data = self.rfile.read(min(READ_SIZE, remaining))
                    if not data:
                        break
                    f.write(data)
                    remaining -= len(data)

            with open(spool, "rb") as sf:
                for hdrs, bstart, bend in PartReader(sf, boundary):
                    disp = hdrs.get("Content-Disposition", "")
                    if not disp:
                        continue
                    # filename* (RFC 5987) wins over filename: it is the only
                    # form that survives non-ASCII names. get_param() lives on
                    # the Message, not on the header object.
                    try:
                        name_star = hdrs.get_param("filename*", header="content-disposition")
                        name_plain = hdrs.get_param("filename", header="content-disposition")
                    except Exception:
                        name_star = name_plain = None
                    if name_star is None and name_plain is None:
                        continue
                    raw_name = name_plain or ""
                    if name_plain and name_plain.lower().startswith("utf-8''"):
                        raw_name = name_plain[7:]
                    if name_star:
                        # "UTF-8''%C3%A9.txt" -> percent-decoded, then sanitised
                        raw_name = str(name_star).split("''", 1)[-1]
                    name = sanitize_filename(raw_name)
                    size = bend - bstart
                    if self.cfg.max_file_size and size > self.cfg.max_file_size:
                        return self.err(413, "file exceeds limit")
                    dest = self.cfg.storage.unique_path(name)
                    sha = hashlib.sha256()
                    written = 0
                    tmpx = dest + ".assembling-%s" % make_slug(secrets.SystemRandom(), 6)
                    try:
                        with open(tmpx, "wb") as out:
                            written = copy_range(spool, bstart, bend, out, [sha])
                            out.flush()
                            os.fsync(out.fileno())
                        os.replace(tmpx, dest)
                    except Exception:
                        try:
                            os.unlink(tmpx)
                        except OSError:
                            pass
                        raise
                    results.append({
                        "name": os.path.basename(dest),
                        "size": written,
                        "sha256": sha.hexdigest(),
                        "url": "/d/" + urllib.parse.quote(os.path.basename(dest)),
                    })
            if not results:
                return self.err(400, "no file part found in the multipart body")
            log("%s -> simple upload %s (%s)", self.client_ip(), results[0]["name"],
                human_bytes(results[0]["size"]))
            self.json({"ok": True, "files": results, **results[0]})
        finally:
            try:
                os.unlink(spool)
            except OSError:
                pass

    def serve_download(self, stored: str) -> None:
        """Serve a file, honouring Range so <video> can seek/scrub."""
        if not self.cfg.serve:
            return self.err(404, "downloads are disabled")
        path = self.cfg.storage.resolve(stored)
        if not path:
            return self.err(404, "not found")
        try:
            st = os.stat(path)
            fh = open(path, "rb")
        except OSError:
            return self.err(404, "not found")

        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/octet-stream":
            # Never let a browser render an uploaded .html/.svg as active
            # content on this origin, or it becomes stored XSS.
            ctype += "; charset=utf-8" if ctype.startswith("text/") else ""
            disp = "attachment"
        else:
            disp = "inline"
        # Images, video, audio and PDFs are safe to render inline.
        if not (ctype.startswith(("image/", "video/", "audio/")) or "pdf" in ctype):
            disp = "attachment"

        with fh:
            size = st.st_size
            start, end = 0, size - 1
            partial = False
            rng = self.headers.get("Range", "")
            if rng and size > 0:
                m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
                if m:
                    g1, g2 = m.group(1), m.group(2)
                    if g1 == "" and g2:
                        # suffix range: last N bytes
                        start = max(0, size - int(g2))
                        end = size - 1
                    elif g1:
                        start = int(g1)
                        end = int(g2) if g2 else size - 1
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    end = min(end, size - 1)
                    partial = True

            length = (end - start + 1) if size else 0
            code = 206 if partial else 200
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Disposition",
                              "%s; filename*=UTF-8''%s" % (disp, urllib.parse.quote(os.path.basename(path))))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            if self.command == "HEAD" or not length:
                return

            fh.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = fh.read(min(READ_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # user closed the tab mid-stream; normal


# ---------------------------------------------------------------------------
# Server + main
# ---------------------------------------------------------------------------


class Reaper(threading.Thread):
    def __init__(self, store: SessionStore, interval: int = 600):
        super().__init__(daemon=True)
        self.store = store
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.store.reap()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, addr, handler, cfg: Config):
        self.cfg = cfg
        super().__init__(addr, handler)


def sanitize_filename(raw: str) -> str:
    name = raw.replace("\\", "/").split("/")[-1].split("\x00")[0]
    name = name.replace("\r", "").replace("\n", "").replace("\t", "")
    name = html.unescape(name)
    name = "".join(ch for ch in name if ch.isprintable())
    name = name.strip().strip(".")  # no traversal, no hidden files
    # Trailing dots/spaces are legal on Linux but break on SMB shares and
    # Windows viewers, and confuse some download managers.
    name = name.rstrip(". ")
    if not name:
        name = "file"
    stem, ext = split_ext(name)
    stem = stem[:180]
    ext = ext[:16]
    return (stem or "file") + ext


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="dropzone.py",
        description="Single-file public upload server for Cloudflare Tunnel.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--host", default="127.0.0.1", help="bind address")
    ap.add_argument("--port", type=int, default=9080, help="port")
    ap.add_argument("--key", default=os.environ.get("DZ_KEY", ""), help="shared secret; empty disables auth")
    ap.add_argument("--dir", default=os.environ.get("DZ_DIR", "./uploads"), help="storage directory")
    ap.add_argument("--subdir", default="", help="store inside this subdirectory of --dir")
    ap.add_argument("--serve", action="store_true", default=True, help="allow downloads via /d/")
    ap.add_argument("--no-serve", dest="serve", action="store_false", help="disable downloads")
    ap.add_argument("--max-file-size", type=int, default=0, help="per-file cap in bytes (0=unlimited)")
    ap.add_argument("--max-files", type=int, default=0, help="total cap in bytes for --dir (0=unlimited)")
    ap.add_argument("--chunked", action="store_true", default=True, help="enable resumable chunked uploads")
    ap.add_argument("--no-chunked", dest="chunked", action="store_false")
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="bytes per chunk")
    ap.add_argument("--parallel", type=int, default=2, help="parallel chunk requests per file")
    ap.add_argument("--rate-limit", type=int, default=240, help="control-plane requests/min per IP (0=off)")
    ap.add_argument("--rate-burst", type=int, default=60, help="control-plane burst per IP")
    ap.add_argument("--chunk-rate-limit", type=int, default=0,
                    help="chunk PUTs/min per IP (default: 12x --rate-limit)")
    ap.add_argument("--chunk-rate-burst", type=int, default=0,
                    help="chunk PUT burst per IP (default: 600)")
    ap.add_argument("--trust-proxy", action="store_true", default=True, help="trust CF/X-Forwarded-* headers")
    ap.add_argument("--no-trust-proxy", dest="trust_proxy", action="store_false")
    ap.add_argument("--hosts", default="", help="comma-separated Host allowlist")
    ap.add_argument("--insecure", action="store_true", help="acknowledge running without a key")
    ap.add_argument("--print-url", default="", help="print this public URL on start")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    cfg = Config(args)

    if not cfg.key and not args.insecure:
        sys.stderr.write(
            "ERROR: no --key given. This server will be reachable from the entire\n"
            "       internet the moment a tunnel points at it. Pass --key SECRET,\n"
            "       or --insecure to accept the risk.\n"
        )
        return 2

    chunk = max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, cfg.chunk_size))
    if chunk > CLOUDFLARE_BODY_LIMIT:
        chunk = DEFAULT_CHUNK_SIZE
    cfg.chunk_size = chunk
    cfg.parallel = max(1, min(6, cfg.parallel))

    srv = Server((args.host, args.port), Handler, cfg)
    reaper = Reaper(cfg.sessions)
    reaper.start()

    banner = [
        "",
        "  dropzone ready",
        "    listening   http://%s:%d" % (args.host, args.port),
        "    storage     %s" % cfg.storage.dir,
        "    auth        %s" % ("key required" if cfg.key else "DISABLED (--insecure)"),
        "    uploads     %s  (chunk %s)" % (
            "resumable/chunked" if cfg.chunked else "single-shot", human_bytes(chunk)),
        "    per-file    %s" % (human_bytes(cfg.max_file_size) if cfg.max_file_size else "unlimited"),
        "    proxy       %s" % ("trusted (CF/X-Forwarded)" if cfg.trust_proxy else "direct"),
        "    files       %d  (%s)" % (len(cfg.storage.list(9999)), human_bytes(cfg.storage.allocated())),
    ]
    if args.print_url:
        banner.append("    public      %s" % args.print_url)
    banner += ["", "  Tunnel:  cloudflared tunnel --url http://%s:%d" % (args.host, args.port), ""]
    sys.stderr.write("\n".join(banner) + "\n")
    sys.stderr.flush()

    def shutdown(signum, frame):
        log("signal %s, shutting down", signum)
        reaper.stop()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass

    try:
        srv.serve_forever(poll_interval=0.4)
    finally:
        reaper.stop()
        srv.server_close()
    log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())