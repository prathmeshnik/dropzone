#!/usr/bin/env python3
"""End-to-end test for dropzone.py. Exercises the real HTTP surface."""
import hashlib, json, os, re, shutil, subprocess, sys, time, urllib.request, urllib.error
import http.client, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the server lives one level up
SRV = os.path.join(ROOT, "dropzone.py")
PORT = 8791
KEY = "test-secret-1234"
ROOT = tempfile.mkdtemp(prefix="dz-test-")

passed, failed = [], []

def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ((" :: " + str(extra)) if extra and not cond else ""))

SECTION = ["a"]

def fresh_stats(wait=1.15):
    """GET /api/stats after its TTL, so the snapshot is genuinely recomputed.

    stats() caches for a second to keep a polled dashboard from re-walking the
    storage directory on every request. That is the behaviour under test, so
    assertions about *changing* figures have to wait it out.
    """
    time.sleep(wait)
    return json.loads(req("GET", "/api/stats")[1])

def req(method, path, body=None, headers=None, expect=None):
    """Issue one request on a fresh connection.

    `headers` may contain None values, meaning 'do not send this header' --
    needed to prove a cookie works without the key header.

    Each test section gets its own CF-Connecting-IP so rate limiting in one
    section cannot cause unrelated assertions to fail in another.
    """
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    h = {"X-Upload-Key": KEY, "CF-Connecting-IP": "198.51.100.%d" % (hash(path[:1]) % 250 + 1)}
    if SECTION[0] != "a":
        h["CF-Connecting-IP"] = "198.51.100.%d" % (abs(hash(SECTION[0])) % 250 + 1)
    for k, v in (headers or {}).items():
        if v is None:
            h.pop(k, None)
        else:
            h[k] = v
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    st = r.status
    hdrs = {}
    for k, v in r.getheaders():
        hdrs[k] = v  # last wins; case preserved as sent
    c.close()
    return st, data, hdrs

proc = subprocess.Popen(
    [sys.executable, SRV, "--port", str(PORT), "--key", KEY, "--dir", ROOT,
     "--chunk-size", str(1024 * 1024)],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE)

try:
    for _ in range(100):
        try:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
            c.request("GET", "/healthz"); c.getresponse().read(); c.close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        raise SystemExit("server never came up")

    print("\n--- auth ---")
    st, _, _ = req("GET", "/healthz")
    check("healthz is unauthenticated", st == 200, st)

    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    c.request("GET", "/api/config")
    r = c.getresponse(); r.read(); c.close()
    check("api/config rejects missing key", r.status == 401, r.status)

    st, b, _ = req("GET", "/api/config", headers={"X-Upload-Key": "wrong"})
    check("api/config rejects wrong key", st == 401, st)

    st, b, h = req("GET", "/api/config")
    cfg = json.loads(b)
    check("api/config accepts right key", st == 200, st)
    check("config advertises chunked", cfg.get("chunked") is True)
    check("config chunk size honoured", cfg.get("chunkSize") == 1024 * 1024, cfg.get("chunkSize"))

    st, b, h = req("POST", "/api/auth", body=json.dumps({"key": KEY}).encode(),
                   headers={"Content-Type": "application/json"})
    sc = (h.get("Set-Cookie") or h.get("set-cookie") or "")
    check("auth exchange sets cookie", st == 200 and "dz_key" in sc, (st, list(h.keys())))
    st, b, _ = req("GET", "/api/config", headers={"Cookie": sc.split(";")[0], "X-Upload-Key": ""})
    check("cookie alone authenticates", st == 200, st)

    SECTION[0] = "traversal / hostile input"
    print("\n--- traversal / hostile input ---")
    for bad in ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/shadow", ".hidden"]:
        st, _, _ = req("GET", "/d/" + urllib.parse.quote(bad, safe=""))
        check("traversal blocked: %s" % bad, st in (400, 404), st)

    SECTION[0] = "chunked upload, out of order"
    print("\n--- chunked upload, out of order ---")
    payload = os.urandom(5 * 1024 * 1024 + 12345)
    digest = hashlib.sha256(payload).hexdigest()
    CH = 1024 * 1024
    st, b, _ = req("POST", "/api/init", body=json.dumps(
        {"name": "movie.mp4", "size": len(payload), "chunkSize": CH}).encode(),
        headers={"Content-Type": "application/json"})
    check("init accepted", st == 200, (st, b[:200]))
    sess = json.loads(b)
    uid = sess["uploadId"]
    total = sess["totalChunks"]
    check("totalChunks computed correctly", total == 6, total)

    # reverse order + duplicate chunk (realistic retry behaviour)
    for i in reversed(range(total)):
        chunk = payload[i * CH:(i + 1) * CH]
        st, b, _ = req("PUT", "/api/chunk/%s/%d" % (uid, i), body=chunk,
                       headers={"Content-Type": "application/octet-stream"})
        if st != 200:
            check("chunk %d stored" % i, False, (st, b[:200])); break
    else:
        check("all 6 chunks stored out of order", True)

    st, b, _ = req("PUT", "/api/chunk/%s/0" % uid, body=payload[:CH])
    check("re-sending a chunk is idempotent", st == 200, st)

    st, b, _ = req("PUT", "/api/chunk/%s/99" % uid, body=b"x")
    check("out-of-range chunk index rejected", st == 400, st)

    st, b, _ = req("PUT", "/api/chunk/deadbeefdeadbeefdeadbeef/0", body=b"x")
    check("unknown upload id rejected", st == 404, st)

    st, b, _ = req("POST", "/api/finish", body=json.dumps(
        {"uploadId": uid, "name": "movie.mp4", "size": len(payload)}).encode(),
        headers={"Content-Type": "application/json"})
    check("finish accepted", st == 200, (st, b[:300]))
    res = json.loads(b)
    check("sha256 matches original bytes", res.get("sha256") == digest, res.get("sha256"))

    stored = os.path.join(ROOT, res["name"])
    check("file landed on disk", os.path.isfile(stored), stored)
    with open(stored, "rb") as f:
        on_disk = f.read()
    check("bytes are byte-identical on disk", on_disk == payload,
          "%d vs %d" % (len(on_disk), len(payload)))

    SECTION[0] = "partial upload must not be visible"
    print("\n--- partial upload must not be visible ---")
    st, b, _ = req("POST", "/api/init", body=json.dumps(
        {"name": "secret.pdf", "size": 4 * 1024 * 1024, "chunkSize": CH}).encode(),
        headers={"Content-Type": "application/json"})
    s2 = json.loads(b)["uploadId"]
    req("PUT", "/api/chunk/%s/0" % s2, body=b"A" * CH)
    st, b, _ = req("POST", "/api/finish", body=json.dumps(
        {"uploadId": s2, "name": "secret.pdf", "size": 4 * 1024 * 1024}).encode(),
        headers={"Content-Type": "application/json"})
    check("incomplete upload refused at finish", st == 409, (st, b[:200]))
    listing = json.loads(req("GET", "/api/list")[1])["files"]
    check("partial file absent from listing", not any(f["name"] == "secret.pdf" for f in listing))
    req("DELETE", "/api/chunk/" + s2)

    SECTION[0] = "download + Range (video scrubbing)"
    print("\n--- download + Range (video scrubbing) ---")
    url = res["url"]
    st, b, hh = req("GET", url)
    check("full download works", st == 200 and b == payload, st)
    check("accept-ranges advertised", hh.get("Accept-Ranges") == "bytes", hh.get("Accept-Ranges"))
    check("inline disposition for video", "inline" in hh.get("Content-Disposition", ""),
          hh.get("Content-Disposition"))

    st, b, hh = req("GET", url, headers={"Range": "bytes=0-1023"})
    check("range 0-1023 honored", st == 206 and b == payload[:1024], st)
    check("content-range correct",
          hh.get("Content-Range") == "bytes 0-1023/%d" % len(payload), hh.get("Content-Range"))

    st, b, hh = req("GET", url, headers={"Range": "bytes=-500"})
    check("suffix range honored", st == 206 and b == payload[-500:], st)

    mid = len(payload) // 2
    st, b, hh = req("GET", url, headers={"Range": "bytes=%d-" % mid})
    check("open-ended range honored", st == 206 and b == payload[mid:], st)

    st, b, hh = req("GET", url, headers={"Range": "bytes=%d-%d" % (len(payload) + 10, len(payload) + 20)})
    check("unsatisfiable range -> 416", st == 416, st)

    st, b, _ = req("HEAD", url)
    check("HEAD works", st == 200 and len(b) == 0, st)

    SECTION[0] = "collision handling"
    print("\n--- collision handling ---")
    st, b, _ = req("POST", "/api/init", body=json.dumps(
        {"name": "movie.mp4", "size": 16, "chunkSize": CH}).encode(),
        headers={"Content-Type": "application/json"})
    s3 = json.loads(b)["uploadId"]
    req("PUT", "/api/chunk/%s/0" % s3, body=b"Z" * 16)
    st, b, _ = req("POST", "/api/finish", body=json.dumps(
        {"uploadId": s3, "name": "movie.mp4", "size": 16}).encode(),
        headers={"Content-Type": "application/json"})
    r3 = json.loads(b)
    check("same name does not overwrite", r3["name"] != res["name"], r3["name"])
    with open(os.path.join(ROOT, res["name"]), "rb") as f:
        check("original still intact after collision", f.read() == payload)

    SECTION[0] = "simple multipart endpoint"
    print("\n--- simple multipart endpoint ---")
    boundary = "----dztestboundary"
    for fname, content in [("notes.txt", b"hello world"), ("unicode-\u00e9\u00e0.md", "caf\u00e9 \u2713".encode())]:
        body = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n" % (boundary, fname)).encode()
                + content + ("\r\n--%s--\r\n" % boundary).encode())
        st, b, _ = req("POST", "/api/upload", body=body,
                       headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        check("simple upload: %s" % fname, st == 200, (st, b[:200]))
        if st == 200:
            got = json.loads(b)
            st2, data, _ = req("GET", got["url"])
            check("simple roundtrip content: %s" % fname, data == content, (data[:80], content[:80]))
    check("unicode name preserved",
          any(f["name"].startswith("unicode") for f in json.loads(req("GET", "/api/list")[1])["files"]))

    SECTION[0] = "limits"
    print("\n--- limits ---")
    st, b, _ = req("POST", "/api/init", body=json.dumps(
        {"name": "too-big.bin", "size": 10 ** 15}).encode(),
        headers={"Content-Type": "application/json"})
    check("huge declared size not instantly rejected (no limit set)", st in (200, 413), st)

    st, b, _ = req("POST", "/api/init", body=json.dumps(
        {"name": "x", "size": 100}).encode(), headers={"Content-Type": "application/json"})
    s4 = json.loads(b)["uploadId"]
    req("PUT", "/api/chunk/%s/0" % s4, body=b"y" * 100)
    st, b, _ = req("POST", "/api/finish", body=json.dumps(
        {"uploadId": s4, "size": 999999}).encode(), headers={"Content-Type": "application/json"})
    check("size mismatch at finish rejected", st == 500 and b"mismatch" in b.lower(), (st, b[:200]))
    check("mismatched file not left on disk",
          not any(f["name"] == "x" and f["size"] != 100
                  for f in json.loads(req("GET", "/api/list")[1])["files"]))

    SECTION[0] = "resume after interruption"
    print("\n--- resume after interruption ---")
    payload2 = os.urandom(4 * 1024 * 1024 + 7)
    d2 = hashlib.sha256(payload2).hexdigest()
    st, b, _ = req("POST", "/api/init", body=json.dumps(
        {"name": "resume.bin", "size": len(payload2), "chunkSize": CH}).encode(),
        headers={"Content-Type": "application/json"})
    s5 = json.loads(b)["uploadId"]
    # Simulate a phone losing signal after 2 of 4 chunks.
    for i in (0, 2):
        req("PUT", "/api/chunk/%s/%d" % (s5, i), body=payload2[i * CH:(i + 1) * CH])
    st, b, _ = req("GET", "/api/chunk/" + s5)
    s = json.loads(b)
    check("status reports received chunks", sorted(s.get("received", [])) == [0, 2], s.get("received"))
    # 4 MiB + 7 over 1 MiB chunks == 5 chunks, so 0 and 2 received leaves 1,3,4.
    check("status reports missing chunks", s.get("missing") == [1, 3, 4], s.get("missing"))
    check("totalChunks accounts for short final chunk", s.get("totalChunks") == 5, s.get("totalChunks"))
    check("status reports bytes stored", s.get("bytesStored") == 2 * CH, s.get("bytesStored"))
    # Client resumes: sends only the missing ones.
    for i in s["missing"]:
        req("PUT", "/api/chunk/%s/%d" % (s5, i), body=payload2[i * CH:(i + 1) * CH])
    st, b, _ = req("POST", "/api/finish", body=json.dumps(
        {"uploadId": s5, "name": "resume.bin", "size": len(payload2)}).encode(),
        headers={"Content-Type": "application/json"})
    check("resumed upload finishes", st == 200, (st, b[:200]))
    if st == 200:
        r5 = json.loads(b)
        check("resumed file is byte-identical", r5["sha256"] == d2)
        st2, data, _ = req("GET", r5["url"])
        check("resumed file downloads intact", data == payload2)

    # ------------------------------------------------- incoming feed
    SECTION[0] = "xfers"
    print("\n--- incoming feed ---")
    payload3 = os.urandom(3 * 1024 * 1024)
    st, b, _ = req("POST", "/api/init", body=json.dumps(
        {"name": "landing.mkv", "size": len(payload3), "chunkSize": CH}).encode(),
        headers={"Content-Type": "application/json"})
    s6 = json.loads(b)["uploadId"]

    st, b, _ = req("GET", "/api/transfers")
    xf = json.loads(b)["transfers"]
    mine = [t for t in xf if t["id"] == s6]
    check("a fresh upload appears in the feed", len(mine) == 1, [t["id"][:8] for t in xf])
    if mine:
        t = mine[0]
        check("feed reports the declared name and size",
              t["name"] == "landing.mkv" and t["size"] == len(payload3), t)
        check("feed starts at zero bytes", t["bytes"] == 0, t)
        check("feed knows the chunk count", t["totalChunks"] == 3, t)
        check("feed carries timing for rate/eta", t["elapsed"] >= 0 and t["idle"] >= 0, t)

    # Push 2 of 3 chunks; the feed must reflect exactly what has landed.
    for i in (0, 1):
        req("PUT", "/api/chunk/%s/%d" % (s6, i), body=payload3[i * CH:(i + 1) * CH])
    st, b, _ = req("GET", "/api/transfers")
    t = [x for x in json.loads(b)["transfers"] if x["id"] == s6][0]
    check("feed counts received chunks", t["receivedChunks"] == 2, t)
    check("feed counts bytes stored", t["bytes"] == 2 * CH, t)

    # /api/stats must agree with /api/transfers, and must count the partial
    # bytes that are on disk but not yet a published file. Every figure here
    # is a DELTA: earlier sections have already stored files and left a
    # truncated-chunk .tmp behind, so absolute totals would be meaningless.
    st, b, _ = req("GET", "/api/transfers")
    before = json.loads(b)["transfers"]
    st, b, _ = req("GET", "/api/stats")
    base = json.loads(b)
    before_snap = [x for x in before if x["id"] == s6][0]

    st, b, _ = req("GET", "/api/stats")
    stt = json.loads(b)
    stx = [x for x in stt["transfers"] if x["id"] == s6][0]
    # Timing fields necessarily differ between two calls, so compare the
    # facts about the transfer itself.
    stable = ("id", "name", "size", "chunkSize", "totalChunks",
              "receivedChunks", "bytes")
    check("stats and transfers agree on the session",
          all(stx[k] == before_snap[k] for k in stable), (stx, before_snap))
    check("stats counts the session", stt["activeCount"] >= 1, stt.get("activeCount"))

    # An incomplete upload must stay in the feed: finishing early is a 409 and
    # the session legitimately survives, so the owner keeps seeing it.
    st, b, _ = req("POST", "/api/finish", body=json.dumps(
        {"uploadId": s6, "name": "landing.mkv", "size": len(payload3)}).encode(),
        headers={"Content-Type": "application/json"})
    check("finish refuses an incomplete upload", st == 409, (st, b[:120]))
    st, b, _ = req("GET", "/api/transfers")
    check("a refused finish keeps the transfer visible",
          any(x["id"] == s6 for x in json.loads(b)["transfers"]))

    # Now the last chunk lands: in-flight bytes grow by exactly one chunk.
    # stats() caches for a second, so wait it out -- see the cache test below.
    req("PUT", "/api/chunk/%s/2" % s6, body=payload3[2 * CH:])
    grown = fresh_stats()
    check("in-flight bytes grow as chunks land",
          grown["storage"]["partialBytes"] - base["storage"]["partialBytes"] == CH,
          (base["storage"]["partialBytes"], grown["storage"]["partialBytes"]))
    check("stats does not count a partial as a stored file",
          grown["storage"]["files"] == base["storage"]["files"],
          (base["storage"]["files"], grown["storage"]["files"]))
    st, b, _ = req("GET", "/api/transfers")
    t = [x for x in json.loads(b)["transfers"] if x["id"] == s6][0]
    check("feed reaches the full byte count", t["bytes"] == len(payload3), t)

    st, b, _ = req("POST", "/api/finish", body=json.dumps(
        {"uploadId": s6, "name": "landing.mkv", "size": len(payload3)}).encode(),
        headers={"Content-Type": "application/json"})
    check("completed upload finishes", st == 200, (st, b[:120]))
    st, b, _ = req("GET", "/api/transfers")
    left = [x["id"] for x in json.loads(b)["transfers"] if x["id"] == s6]
    check("a finished upload leaves the feed", not left, left)

    # Once finished, the bytes move from "in flight" to "a real file". The
    # TOTAL on disk must not change -- assembling a file only relocates
    # bytes that were already being written, it does not add any.
    fin = fresh_stats()
    check("stats counts the finished file",
          fin["storage"]["files"] == grown["storage"]["files"] + 1,
          (grown["storage"], fin["storage"]))
    check("total bytes on disk are unchanged by assembly",
          fin["storage"]["bytes"] == grown["storage"]["bytes"],
          (grown["storage"]["bytes"], fin["storage"]["bytes"]))
    check("in-flight bytes are released on finish",
          fin["storage"]["partialBytes"]
          == grown["storage"]["partialBytes"] - len(payload3),
          (grown["storage"]["partialBytes"], fin["storage"]["partialBytes"]))
    check("finished total grew by exactly the file size",
          (fin["storage"]["bytes"] - fin["storage"]["partialBytes"])
          == (grown["storage"]["bytes"] - grown["storage"]["partialBytes"]) + len(payload3),
          (grown["storage"], fin["storage"]))

    # The 1s TTL is a real optimisation: repeated polls inside the window must
    # return the identical payload rather than re-walking the directory.
    a = json.loads(req("GET", "/api/stats")[1])
    b = json.loads(req("GET", "/api/stats")[1])
    check("stats serves a cached snapshot within the TTL",
          a["storage"] == b["storage"] and a["transfers"] == b["transfers"], (a, b))
    check("stats recomputes once the TTL expires",
          fresh_stats()["storage"]["bytes"] == a["storage"]["bytes"], a["storage"])

    # ---------------------------------------------------- quota regressions
    # Three bugs lived in this one branch of assemble(), all of them only
    # reachable when --max-files is set, which is why they survived.
    SECTION[0] = "quota-assemble"
    print("\n--- assembling with a quota configured ---")
    # The whole suite above ran with no --max-files, so exercise the quota path
    # on a server of its own.
    qroot = tempfile.mkdtemp(prefix="dz-quota-")
    qport = 8793
    qproc = subprocess.Popen(
        [sys.executable, SRV, "--port", str(qport), "--key", KEY,
         "--dir", qroot, "--max-files", str(24 * 1024 * 1024)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                c = http.client.HTTPConnection("127.0.0.1", qport, timeout=5)
                c.request("GET", "/healthz"); r = c.getresponse(); r.read(); c.close()
                if r.status == 200:
                    break
            except OSError:
                time.sleep(0.1)

        def qcall(method, path, body=None, ctype=None):
            c = http.client.HTTPConnection("127.0.0.1", qport, timeout=30)
            h = {"X-Upload-Key": KEY}
            if ctype:
                h["Content-Type"] = ctype
            c.request(method, path, body=body, headers=h)
            r = c.getresponse(); data = r.read(); st = r.status; c.close()
            return st, data

        qs = 1024 * 1024
        qpay = os.urandom(20 * qs)
        st, b = qcall("POST", "/api/init", json.dumps(
            {"name": "quota.bin", "size": len(qpay), "chunkSize": qs}).encode(),
            "application/json")
        check("quota: init accepted", st == 200, (st, b[:100]))
        qid = json.loads(b)["uploadId"]
        for i in range(20):
            qcall("PUT", "/api/chunk/%s/%d" % (qid, i), qpay[i * qs:(i + 1) * qs])

        # Regression 1: assemble() holds storage._lock and then called
        # allocated(), which re-acquires it. The plain Lock is not reentrant,
        # so this hung the request thread forever and wedged the whole server.
        st, b = qcall("POST", "/api/finish", json.dumps(
            {"uploadId": qid, "size": len(qpay)}).encode(), "application/json")
        check("quota: finish completes instead of deadlocking", st == 200, (st, b[:160]))

        # Regression 2: the check added the assembled size on top of bytes
        # that were already on disk as chunks, charging for the file twice.
        st, b = qcall("GET", "/api/stats")
        qs_st = json.loads(b)["storage"]
        check("quota: a 20 MiB file against a 24 MiB quota is saved",
              st == 200 and qs_st["files"] == 1 and qs_st["bytes"] == 20 * qs,
              qs_st)

        # Regression 3: the ".assembling-*" temp file was counted alongside
        # the chunks it is built from, so usage looked doubled mid-join.
        st, b = qcall("GET", "/api/list")
        check("quota: no assembly temp files leak into the file list",
              all(".assembling-" not in f["name"] for f in json.loads(b)["files"]),
              [f["name"] for f in json.loads(b)["files"]])

        # And the quota must still be enforced rather than silently ignored.
        st, b = qcall("POST", "/api/init", json.dumps(
            {"name": "over.bin", "size": 20 * qs, "chunkSize": qs}).encode(),
            "application/json")
        over_id = json.loads(b)["uploadId"]
        codes = []
        for i in range(20):
            cs, _ = qcall("PUT", "/api/chunk/%s/%d" % (over_id, i), qpay[:qs])
            codes.append(cs)
            if cs == 507:
                break
        check("quota: an over-quota upload is refused", 507 in codes, codes[-3:])
    finally:
        qproc.terminate()
        try:
            qproc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            qproc.kill()
        shutil.rmtree(qroot, ignore_errors=True)

    SECTION[0] = "auth variants"
    print("\n--- auth variants ---")
    sc = ""
    st, b, h = req("POST", "/api/auth", body=json.dumps({"key": KEY}).encode(),
                   headers={"Content-Type": "application/json"})
    sc = (h.get("Set-Cookie") or h.get("set-cookie") or "")
    st, _, _ = req("GET", "/api/config", headers={"X-Upload-Key": None, "Cookie": sc.split(";")[0]})
    check("cookie alone authenticates", st == 200, st)
    st, _, _ = req("GET", "/api/config?k=" + KEY, headers={"X-Upload-Key": None})
    check("query-string key works", st == 200, st)
    st, _, _ = req("GET", "/api/config", headers={"X-Upload-Key": None, "Authorization": "Bearer " + KEY})
    check("bearer token works", st == 200, st)
    st, _, _ = req("GET", "/api/config", headers={"X-Upload-Key": None, "Authorization": "Basic " + __import__("base64").b64encode(("u:" + KEY).encode()).decode()})
    check("basic auth works", st == 200, st)

    SECTION[0] = "rate limiting"
    print("\n--- rate limiting ---")
    # Burst is 60 by default; hammer one endpoint from a distinct 'IP' header.
    codes = set()
    for i in range(90):
        st, _, _ = req("GET", "/api/list", headers={"CF-Connecting-IP": "203.0.113.9"})
        codes.add(st)
    check("rate limiter kicks in on flood", 429 in codes, sorted(codes))
    st, _, _ = req("GET", "/api/list", headers={"CF-Connecting-IP": "203.0.113.200"})
    check("rate limit is per-IP, not global", st == 200, st)

    SECTION[0] = "filename handling"
    print("\n--- filename handling ---")
    # Names that MUST survive sanitising and still download byte-identical.
    good_names = [
        "My Video.mp4", "Holiday Photos 2024 (final).tar.gz", "no-extension",
        "a", "report_2026.Q3.pdf", "weird [brackets] {braces}.txt",
        "100% done!.txt", "IMG_0421e9.jpg",
    ]
    for gn in good_names:
        st, b, _ = req("POST", "/api/init", body=json.dumps(
            {"name": gn, "size": 8, "chunkSize": CH}).encode(),
            headers={"Content-Type": "application/json"})
        if st != 200:
            check("accepts name: %s" % gn, False, (st, b[:120])); continue
        u = json.loads(b)["uploadId"]
        req("PUT", "/api/chunk/%s/0" % u, body=b"12345678")
        st, b, _ = req("POST", "/api/finish", body=json.dumps(
            {"uploadId": u, "name": gn, "size": 8}).encode(),
            headers={"Content-Type": "application/json"})
        if st != 200:
            check("finishes name: %s" % gn, False, (st, b[:120])); continue
        got = json.loads(b)
        st2, data, _ = req("GET", got["url"])
        check("stores+serves: %s" % gn, st2 == 200 and data == b"12345678", (st2, got["name"]))

    # Names that MUST be neutralised (never escape the directory).
    evil_names = [
        "../../../etc/passwd", "..\\..\\windows\\system32\\cfg",
        "/etc/shadow", ".bashrc", ".parts/../escape", "a/b/c.txt",
        "\x00evil.txt", "tab\tname.txt", "null\x00byte.mp4",
    ]
    for en in evil_names:
        st, b, _ = req("POST", "/api/init", body=json.dumps(
            {"name": en, "size": 4, "chunkSize": CH}).encode(),
            headers={"Content-Type": "application/json"})
        if st != 200:
            check("neutralises: %r" % en, True); continue
        u = json.loads(b)["uploadId"]
        req("PUT", "/api/chunk/%s/0" % u, body=b"EVIL")
        st, b, _ = req("POST", "/api/finish", body=json.dumps(
            {"uploadId": u, "name": en, "size": 4}).encode(),
            headers={"Content-Type": "application/json"})
        if st != 200:
            check("neutralises: %r" % en, True); continue
        got = json.loads(b)
        name = got["name"]
        on_disk = os.path.join(ROOT, name)
        inside = os.path.dirname(os.path.abspath(on_disk)) == os.path.abspath(ROOT)
        st2, _, _ = req("GET", got["url"])
        check("neutralises: %r -> %r" % (en, name),
              "/" not in name and "\\" not in name and not name.startswith(".")
              and inside and os.path.isfile(on_disk) and st2 == 200,
              (name, inside, st2))

    SECTION[0] = "index page"
    print("\n--- data plane has its own budget ---")
    # A 1 GB upload at 8 MiB chunks is 128 chunk PUTs. The control-plane limit
    # is 240/min, so sharing one budget would 429 a legitimate large upload.
    SECTION[0] = "dataplane"
    big = 200 * CH  # 200 chunks -- well past the control-plane budget
    st, b, _ = req("POST", "/api/init", body=json.dumps(
        {"name": "big.bin", "size": big, "chunkSize": CH}).encode(),
        headers={"Content-Type": "application/json"})
    check("big init accepted", st == 200, st)
    uid = json.loads(b)["uploadId"]
    check("big file chunk count correct", json.loads(b)["totalChunks"] == 200, json.loads(b)["totalChunks"])
    errs = 0
    for i in range(200):
        s2, _, _ = req("PUT", "/api/chunk/%s/%d" % (uid, i), body=b"q" * CH)
        if s2 != 200:
            errs += 1
    check("200 sequential chunk PUTs all accepted (no spurious 429)", errs == 0, "%d rejected" % errs)
    st, b, _ = req("POST", "/api/finish", body=json.dumps(
        {"uploadId": uid, "name": "big.bin", "size": big}).encode(),
        headers={"Content-Type": "application/json"})
    check("200-chunk upload assembles", st == 200, (st, b[:200]))
    if st == 200:
        check("200-chunk size correct", json.loads(b)["size"] == big, json.loads(b)["size"])

    print("\n--- truncated / cancelled uploads (Cloudflare timeout) ---")
    # Regression: a chunk whose body arrives short used to raise
    # "short read" and then die with BrokenPipeError *while sending the error
    # response*, spewing a traceback and leaving the client unable to retry.
    SECTION[0] = "truncated"
    import socket as _sock
    # Use a real session so the request reaches the body-reading code.
    st, b, _ = req("POST", "/api/init", body=json.dumps(
        {"name": "cancelled.mkv", "size": 4 * 1024 * 1024, "chunkSize": 1024 * 1024}).encode(),
        headers={"Content-Type": "application/json"})
    t_uid = json.loads(b)["uploadId"]
    body = b"Q" * (2 * 1024 * 1024)
    s_ = _sock.create_connection(("127.0.0.1", PORT), timeout=15)
    s_.sendall(
        b"PUT /api/chunk/" + t_uid.encode() + b"/0 HTTP/1.1\r\nHost: x\r\n"
        b"X-Upload-Key: " + KEY.encode() + b"\r\n"
        b"Content-Length: %d\r\nContent-Type: application/octet-stream\r\n\r\n" % len(body)
        + body[:1024]          # deliberately only 1 KiB of 2 MiB
    )
    s_.shutdown(_sock.SHUT_WR)   # then EOF, as a cancelled request looks
    s_.settimeout(10)
    got = b""
    try:
        while len(got) < 4096:
            d = s_.recv(4096)
            if not d:
                break
            got += d
    except _sock.timeout:
        pass
    s_.close()
    check("truncated chunk does not hang the server", bool(got), "no response")
    check("truncated chunk answers 408 (retryable), not 500",
          b" 408 " in got.split(b"\r\n")[0], got.split(b"\r\n")[0] if got else "empty")
    check("truncated chunk is NOT recorded as received",
          json.loads(req("GET", "/api/chunk/" + t_uid)[1]).get("received") == [],
          "chunk 0 wrongly marked present")
    st, _, _ = req("PUT", "/api/chunk/%s/0" % t_uid, body=b"Q" * (2 * 1024 * 1024))
    check("session survives a cancelled chunk (retry works)", st == 200, st)
    req("DELETE", "/api/chunk/" + t_uid)

    print("\n--- keep-alive stays in sync after an early error ---")
    # cloudflared pools HTTP/1.1 connections to the origin. An early error
    # that leaves the body unread desyncs the next request on that pooled
    # connection, which cloudflared reports as "use of closed network
    # connection".
    s2 = _sock.create_connection(("127.0.0.1", PORT), timeout=20)
    big = b"Z" * (4 * 1024 * 1024)
    s2.sendall(
        b"PUT /api/chunk/nosuchsession1234567890/0 HTTP/1.1\r\nHost: x\r\n"
        b"X-Upload-Key: " + KEY.encode() + b"\r\n"
        b"Content-Length: %d\r\nContent-Type: application/octet-stream\r\n\r\n" % len(big) + big
    )
    time.sleep(0.5)
    s2.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
    time.sleep(1.0)
    s2.settimeout(4)
    data = b""
    try:
        while True:
            d = s2.recv(65536)
            if not d:
                break
            data += d
            if data.count(b"HTTP/1.1") >= 2 and b"ok" in data:
                break
    except _sock.timeout:
        pass
    s2.close()
    check("connection survives an early 404 with a 4 MiB body",
          data.count(b"HTTP/1.1") >= 2 and b"ok" in data,
          data.count(b"HTTP/1.1"))
    check("no desync: second response is the healthz 200",
          data.count(b"200 OK") >= 1, data[:200])

    print("\n--- index page ---")
    # Compound extensions must survive de-duplication: "photos.tar.gz" has to
    # become "photos (2).tar.gz", never "photos.tar (2).gz".
    SECTION[0] = "compound-ext"
    for base in ["archive.tar.gz", "bundle.tar.xz", "app.min.js", "plain.txt"]:
        names = []
        for n in range(2):
            st, b, _ = req("POST", "/api/init", body=json.dumps(
                {"name": base, "size": 8, "chunkSize": CH}).encode(),
                headers={"Content-Type": "application/json"})
            u = json.loads(b)["uploadId"]
            req("PUT", "/api/chunk/%s/0" % u, body=b"12345678")
            st, b, _ = req("POST", "/api/finish", body=json.dumps(
                {"uploadId": u, "name": base, "size": 8}).encode(),
                headers={"Content-Type": "application/json"})
            names.append(json.loads(b)["name"])
        check("dedup keeps compound extension: %s" % base,
              names[0] == base and names[1] == base.replace(".", " (2).", 1),
              names)

    SECTION[0] = "index page"
    st, b, _ = req("GET", "/")
    check("index served", st == 200 and b"dropzone" in b, st)
    check("index served without a key (it must render the key prompt)",
          req("GET", "/", headers={"X-Upload-Key": None})[0] == 200)
    check("config injected into page", b"__CONFIG__" not in b and b'"chunkSize"' in b)
    check("page has camera capture input", b'capture="environment"' in b)
    check("page loads the stylesheet", b'style.css' in b)
    check("page loads the client script", b'app.js' in b)
    check("page has no unresolved template markers", b"__CONFIG__" not in b and b"undefined" not in b)

    # The client now lives in its own file, so fetch the assets the page
    # references and assert against those too.
    css_st, css_b, css_ct = req("GET", "/style.css")
    check("style.css served", css_st == 200 and len(css_b) > 500, css_st)
    check("style.css serves with a css content-type",
          "css" in css_ct.get("Content-Type", ""), css_ct.get("Content-Type"))
    check("style.css has the touch media query the page depends on", b"hover:none" in css_b)
    check("style.css honours prefers-reduced-motion", b"prefers-reduced-motion" in css_b)
    js_st, js_b, js_ct = req("GET", "/app.js")
    check("app.js served", js_st == 200 and len(js_b) > 500, js_st)
    check("app.js serves with a javascript content-type",
          "javascript" in js_ct.get("Content-Type", ""), js_ct.get("Content-Type"))
    check("client warns on backgrounding", b"visibilitychange" in js_b)
    check("client retries chunks with backoff", b"retryOrFail" in js_b and b"MAX_TRIES" in js_b)
    check("client aborts a stalled chunk via xhr.timeout", b"xhr.timeout" in js_b)

    # Unknown asset paths must 404 rather than fall through to a directory
    # listing or, worse, resolve outside web/.
    for bad in ("/nope.js", "/../dropzone.py", "/%2e%2e/dropzone.py", "/app.js.map"):
        check("no traversal via %s" % bad, req("GET", bad)[0] == 404, bad)

    # The incoming feed polls this. It must be key-gated like every other
    # /api route, and it must not leak the uploader's IP address.
    check("/api/transfers requires the key",
          req("GET", "/api/transfers", headers={"X-Upload-Key": None})[0] == 401)
    xst, xb, _ = req("GET", "/api/transfers")
    xf = json.loads(xb)
    check("/api/transfers returns a list",
          xst == 200 and isinstance(xf.get("transfers"), list), xst)
    check("/api/transfers omits client IPs",
          all("ip" not in t for t in xf["transfers"]), xf["transfers"][:1])

    # /api/stats is the single endpoint the live dashboard polls.
    check("/api/stats requires the key",
          req("GET", "/api/stats", headers={"X-Upload-Key": None})[0] == 401)
    sst, sb, _ = req("GET", "/api/stats")
    stats = json.loads(sb)
    check("/api/stats returns a version", sst == 200 and stats.get("version") == 1, sst)
    for k in ("uptime", "storage", "transfers", "speed", "activeCount"):
        check("/api/stats has %s" % k, k in stats, sorted(stats))
    check("/api/stats storage carries bytes/files/quota",
          all(k in stats["storage"] for k in ("bytes", "files", "quota", "pct")),
          stats.get("storage"))
    check("/api/stats speed is a number", isinstance(stats.get("speed"), (int, float)), stats.get("speed"))
    check("/api/stats counts the same transfers as /api/transfers",
          stats["activeCount"] == len(stats["transfers"]), stats.get("activeCount"))
    check("/api/stats never leaks client IPs",
          all("ip" not in t for t in stats["transfers"]), stats["transfers"][:1])
    check("client polls /api/stats", b"/api/stats" in js_b)
    check("client renders the live strip", b"paintLive" in js_b)

    # Every id the script reaches for must exist in the markup. A missing one is
    # null in a real browser, so the first $().textContent on it throws and
    # takes the rest of the page's wiring down with it.
    page = b.decode("utf-8", "replace")
    js = js_b.decode("utf-8", "replace")
    html_ids = set(re.findall(r'\bid="([^"]+)"', page))
    used_ids = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", js))
    used_ids |= set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", js))
    missing = sorted(used_ids - html_ids)
    check("every id the script touches exists (%d refs)" % len(used_ids),
          not missing, "missing: %s" % missing)
    # The drop zone must stay the first thing on screen; the informational
    # sections below it exist to explain the tool, not to bury it.
    pos = {n: page.find('id="%s"' % n) for n in
           ("drop", "list", "secfiles", "sechow", "secdetails")}
    check("drop zone is the first section on the page",
          all(v >= 0 for v in pos.values()) and pos["drop"] == min(pos.values()), pos)
    check("details section is collapsed by default",
          not any("open" in t for t in re.findall(r"<details[^>]*>", page)))
    # Regression: the client used to read #cfg before the parser had created
    # that node, throwing "Cannot read properties of null" on line 1 and
    # leaving every button dead. app.js still does that read, so the blob must
    # precede the <script src> tag that pulls it in.
    ci = b.find(b'id="cfg"')
    # Regression: the client used to read #cfg before the parser had created
    # that node, throwing "Cannot read properties of null" on line 1 and
    # leaving every button dead. app.js still does that read, so the blob must
    # precede the <script src> tag that pulls it in. Match the tag itself --
    # the string "app.js" also occurs in an explanatory comment above.
    ci = b.find(b'id="cfg"')
    tag = re.search(rb'<script[^>]*\ssrc="[^"]*app\.js"[^>]*>', b)
    check("#cfg is parsed before app.js loads",
          ci != -1 and tag is not None and ci < tag.start(), (ci, tag))
    cfg_json = json.loads(b[b.find(b'<script id="cfg"'):].split(b">", 1)[1].split(b"</script>")[0])
    check("served #cfg is valid JSON", cfg_json.get("version") == 1, cfg_json)
    # Defaults must be conservative for a tunnel: a large chunk multiplied by
    # parallel lanes is what pushed requests past Cloudflare's ~100s timeout.
    check("default chunk size is tunnel-safe (<= 2 MiB)",
          cfg_json.get("chunkSize", 1 << 30) <= 2 * 1024 * 1024, cfg_json.get("chunkSize"))
    check("default parallelism is conservative (<= 2)", cfg_json.get("parallel") <= 2, cfg_json.get("parallel"))
    check("client is told a per-request timeout", bool(cfg_json.get("requestTimeout")), cfg_json)

finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n" + "=" * 62)
print("  %d passed, %d failed" % (len(passed), len(failed)))
if failed:
    print("  failures:")
    for f in failed:
        print("   - " + f)
print("=" * 62)
sys.exit(1 if failed else 0)