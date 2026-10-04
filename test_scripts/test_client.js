/**
 * Fetches the page the server ACTUALLY serves, runs its JavaScript inside a
 * minimal DOM shim, and verifies a multi-chunk file lands byte-identical.
 * This exercises the real pump()/sendChunk()/finish() upload path.
 *
 * It deliberately fetches over HTTP rather than extracting INDEX_HTML from
 * the python source: that keeps the __CONFIG__ substitution and the real
 * document element ordering under test, which is where the reported
 * "Cannot read properties of null (reading 'textContent')" bug lived.
 *
 *   node test_client.js <base-url> <key> [bytes] [filename]
 */
const http = require('http');
const vm = require('vm');

const BASE = process.argv[2] || 'http://127.0.0.1:9080';
const KEY = process.argv[3] || 'demo-key';
const SIZE = Number(process.argv[4] || (5 * 1024 * 1024 + 4321));
const FILENAME = process.argv[5] || 'IMG_0421 video \u00e9.mp4';

// If argv[5] names a file that exists on disk, upload its real bytes; that is
// what makes this a real-world test. Otherwise fall back to synthesising a
// deterministic buffer so the script is still usable as a smoke test.
const fs = require('fs');
const path = require('path');
let REAL = null;
try {
  const st = fs.statSync(FILENAME);
  if (st.isFile()) REAL = {buf: fs.readFileSync(FILENAME), name: path.basename(FILENAME)};
} catch (e) { /* not a path: use the synthetic buffer */ }

(async () => {
  const res = await fetch(BASE + '/', {headers: {'X-Upload-Key': KEY}});
  if (res.status !== 200) {
    console.error('FAIL: GET / returned', res.status, '(is the key right?)');
    process.exit(1);
  }
  const html = await res.text();

  // The client lives in its own file now, so fetch it the way the browser
  // would rather than scraping it out of the markup.
  const tag = html.match(/<script[^>]*\ssrc="([^"]*app\.js)"[^>]*>/);
  if (!tag) {
    console.error('FAIL: page does not reference app.js -- the client is not wired up');
    process.exit(1);
  }
  const jsRes = await fetch(BASE + tag[1], {headers: {'X-Upload-Key': KEY}});
  if (jsRes.status !== 200) {
    console.error('FAIL: GET ' + tag[1] + ' returned', jsRes.status);
    process.exit(1);
  }
  main(html, await jsRes.text());
})().catch(e => { console.error('fatal:', e && e.stack || e); process.exit(1); });

function main(html, clientJs) {
const cfgTag = html.match(/<script id="cfg"[^>]*>([\s\S]*?)<\/script>/);
if (!cfgTag) { console.error('FAIL: no #cfg script in the served page'); process.exit(1); }
if (cfgTag[1].indexOf('__CONFIG__') !== -1) {
  console.error('FAIL: __CONFIG__ was never substituted into the page');
  process.exit(1);
}
let CFG;
try { CFG = JSON.parse(cfgTag[1]); }
catch (e) { console.error('FAIL: served #cfg is not valid JSON:', e.message); process.exit(1); }

// #cfg must appear before the <script src> tag that loads the client, because
// the client reads it synchronously on its first line. Match the tag itself --
// the string "app.js" also occurs in an explanatory comment further up.
const cfgAt = html.indexOf('id="cfg"');
const tagAt = html.search(/<script[^>]*\ssrc="[^"]*app\.js"/);
if (cfgAt < 0 || tagAt < 0) {
  console.error('FAIL: could not locate #cfg (' + cfgAt + ') or the app.js tag (' + tagAt + ')');
  process.exit(1);
}
if (cfgAt > tagAt) {
  console.error('FAIL: #cfg appears after the app.js tag; the client would read null');
  process.exit(1);
}

let src = clientJs;
// test-only exports; the shipped client is untouched
src += '\nglobalThis.__T = {addFiles, jobs, CFG, pump, start, loadPending, forgetPending, fingerprint, paintInfo, renderFiles, renderTransfers, paintOverall, human, ago};';

const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---- DOM shim -----------------------------------------------------------
function El(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(), _q: {}, _h: {}, children: [], dataset: {},
    style: new Proxy({}, {set: (t, k, v) => (t[k] = v, true)}),
    textContent: '', title: '', hidden: false, value: '', files: [],
    classList: {
      _s: new Set(),
      add: c => el.classList._s.add(c), remove: c => el.classList._s.delete(c),
      contains: c => el.classList._s.has(c),
      toggle: (c, f) => f ? el.classList._s.add(c) : el.classList._s.delete(c),
    },
    appendChild(c) { el.children.push(c); return c; },
    prepend(c) { el.children.unshift(c); return c; },
    remove() { el.removed = true; },
    addEventListener(t, fn) { (this._h[t] = this._h[t] || []).push(fn); },
    removeEventListener() {},
    fire(t, ev) { for (const fn of (this._h[t] || [])) fn(ev || {preventDefault() {}, stopPropagation() {}, target: el}); },
    closest() { return null; },
    // Assigning innerHTML detaches existing children in a real DOM; the shim
    // must do the same or "clear then re-render" code silently accumulates.
    set innerHTML(v) { el._html = v; el.children.length = 0; },
    get innerHTML() { return el._html || ''; },
    querySelector(sel) { return el._q[sel] || (el._q[sel] = El('x')); },
    querySelectorAll() { return []; },
    click() { el.fire('click', {preventDefault() {}, stopPropagation() {}, target: el}); },
  };
  return el;
}
// Model the browser's parser honestly: at the moment the client script runs,
// only elements that appear BEFORE its <script src> tag exist. app.js is the
// last thing in <body>, so in practice the whole document is parsed -- but keep
// deriving the boundary from the markup rather than assuming it.
const bootIdx = html.search(/<script[^>]*\ssrc=/);
if (bootIdx < 0) throw new Error('could not locate the app.js <script src> tag in the page');
const seen = html.slice(0, bootIdx);
const byId = {};
for (const m of seen.matchAll(/\bid="([^"]+)"/g)) {
  const el = El('div');
  if (m[1] === 'cfg') {
    // real <script type="application/json"> content: skip past the '>' of
    // the opening tag (offsets are relative to `seen`).
    const gt = seen.indexOf('>', m.index);
    const start = gt + 1;
    el.textContent = seen.slice(start, seen.indexOf('</script>', start));
    try { JSON.parse(el.textContent); }
    catch (e) { throw new Error('#cfg is not valid JSON in the served page: ' + e.message); }
  }
  byId['#' + m[1]] = el;
}
const missingCfg = byId['#cfg'] === undefined;
if (missingCfg) {
  console.error('FAIL #cfg <script> appears AFTER the script that reads it ' +
    '(offset ' + html.indexOf('id="cfg"') + ' vs script at ' + bootIdx + ')');
  process.exit(1);
}
console.log('#cfg resolves before script execution: ok');

const store = {};
// The page reads the key from localStorage at load time; seed it so the
// shimmed client authenticates exactly like a real browser would.
store['dz_key'] = KEY;
const localStorage = {
  getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; }, clear: () => { for (const k in store) delete store[k]; },
};

// File/FileList stand-ins. slice() returns a range marker the XHR shim reads.
class FakeFile {
  constructor(buf, name) { this.buf = buf; this.name = name; this.size = buf.length; this.lastModified = Date.now(); }
  slice(a, b) { return { __range: [a, b], size: Math.max(0, (b === undefined ? this.size : b) - a), type: '' }; }
}
// A <input type=file> has no meaningful value, but .click() must reach a
// registered handler -- that is exactly what "Choose files" does.
const INPUT_CLICKS = [];
class FakeInput {
  constructor(id) {
    this.id = id; this.files = []; this.value = ''; this.hidden = false; this.accept = '';
    this._h = {}; this.dataset = {}; this.type = 'file';
  }
  addEventListener(t, fn) { (this._h[t] = this._h[t] || []).push(fn); }
  removeEventListener() {}
  fire(t, ev) { for (const fn of (this._h[t] || [])) fn(ev || {}); }
  click() { INPUT_CLICKS.push(this.id); }
}
for (const id of ['picker', 'camfile', 'camimg']) {
  const inp = new FakeInput(id);
  byId['#' + id] = inp;
}

// XMLHttpRequest over node http, honouring upload progress events.
class XHR {
  constructor() { this.headers = {}; this.upload = { onprogress: null }; this.status = 0; this.responseText = ''; }
  open(m, url) { this._m = m; this._u = url; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  _abs(path) { return BASE + path; }
  send(body) {
    const u = new URL(this._abs(this._u));
    const payload = body && body.__range
      ? CURRENT_FILE.buf.slice(body.__range[0], body.__range[1])
      : (body || Buffer.alloc(0));
    const opts = {
      hostname: u.hostname, port: u.port, path: u.pathname + u.search, method: this._m,
      headers: Object.assign({ 'Content-Length': payload.length }, this.headers),
    };
    if (this.upload.onprogress) this.upload.onprogress({ lengthComputable: true, loaded: payload.length, total: payload.length });
    const req = http.request(opts, res => {
      const bufs = [];
      res.on('data', d => bufs.push(d));
      res.on('end', () => {
        this.status = res.statusCode;
        this.responseText = Buffer.concat(bufs).toString();
        if (this.onload) this.onload();
      });
    });
    req.on('error', () => { if (this.onerror) this.onerror(); });
    req.end(payload);
    this._req = req;
  }
  abort() { try { this._req && this._req.destroy(); } catch (e) {} if (this.onabort) this.onabort(); }
}

let CURRENT_FILE = null;

const sandbox = {
  console, URL, FormData: globalThis.FormData, Blob: globalThis.Blob,
  crypto: globalThis.crypto, JSON, Math, Date, Set, Map, Object, Array, Promise,
  setTimeout, clearTimeout, parseInt, parseFloat, isNaN, String, Number, Error,
  document: {
    hidden: false,
    getElementById: id => byId['#' + id] || El('div'),
    querySelector: s => byId[s] || El('div'),
    querySelectorAll: () => [],
    createElement: t => El(t),
    addEventListener() {},
  },
  window: { addEventListener() {} },
  navigator: { onLine: true },
  matchMedia: q => ({ matches: false, media: q, addEventListener() {} }),
  localStorage, sessionStorage: localStorage,
  XMLHttpRequest: XHR,
  fetch: (u, o) => fetch(u.startsWith('http') ? u : BASE + u, o),
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

const ctx = vm.createContext(sandbox);
vm.runInContext(src, ctx, {filename: 'dropzone-client.js'});

const T = sandbox.__T;
console.log('client JS loaded. CFG =', JSON.stringify(T.CFG));

// Reproduce the reported bug class: if the script threw before wiring up
// listeners, #cfg reads null and *every* button is dead. Assert the handlers
// exist and actually reach the file input.
function assertWired(sel, label) {
  const el = byId[sel];
  const n = (el._h && el._h.click && el._h.click.length) || 0;
  if (!n) { console.error('FAIL: no click handler on ' + sel + ' (' + label + ') -- page is dead'); process.exit(1); }
  return n;
}
assertWired('#pick', 'Choose files button');
assertWired('#cam', 'camera button');
if (!((byId['#gateform']._h || {}).submit || []).length) {
  console.error('FAIL: no submit handler on #gateform -- page is dead');
  process.exit(1);
}
byId['#pick'].fire('click', {preventDefault() {}, stopPropagation() {}});
if (!INPUT_CLICKS.includes('picker')) {
  console.error('FAIL: "Choose files" did not open the file input (got ' + JSON.stringify(INPUT_CLICKS) + ')');
  process.exit(1);
}
console.log('choose-files wiring: ok (reached #picker)');

// ---- the informational panels ------------------------------------------
// paintInfo()/renderFiles() run on every page load but are easy to break with
// a stray DOM call, and a throw in either leaves the page half-decorated. Drive
// them here with representative data.
function expectThrow(label, fn) {
  try { fn(); } catch (e) {
    console.error('FAIL: ' + label + ' threw: ' + e.message);
    process.exit(1);
  }
}
expectThrow('paintInfo()', () => T.paintInfo());
expectThrow('renderFiles([])', () => T.renderFiles([]));
const emptyHint = (byId['#fileshint'] || {}).textContent;
expectThrow('renderFiles(rows)', () => T.renderFiles([
  {name: 'a.mkv', size: 48 * 1048576, mtime: Date.now() / 1000 - 30},
  {name: 'IMG_0421\u00e9.jpg', size: 1024, mtime: Date.now() / 1000 - 7200},
  {name: 'report.pdf', size: 7, mtime: Date.now() / 1000 - 864000 * 3},
]));
// Read the rendered result immediately: the boot-time refreshFiles() fetch
// resolves on its own schedule and would overwrite this a tick later.
const rows = (byId['#files'].children || []).length;
const hint = (byId['#fileshint'] || {}).textContent;
if (!CFG.serveDownloads) {
  // --no-serve: the panel must say so instead of listing unopenable files.
  if (rows !== 0 || !/disabled/.test(String(hint))) {
    console.error('FAIL: --no-serve still rendered rows (rows=' + rows + ', hint=' + JSON.stringify(hint) + ')');
    process.exit(1);
  }
} else if (rows !== 3 || !/3 files/.test(String(hint))) {
  console.error('FAIL: file list rendered ' + rows + ' rows, hint=' + JSON.stringify(hint));
  process.exit(1);
}
if (CFG.serveDownloads && !String(emptyHint).match(/empty/)) {
  console.error('FAIL: empty file list gave no hint (got ' + JSON.stringify(emptyHint) + ')');
  process.exit(1);
}
console.log('info panels: ok (' + T.human(1536) + ' / "' + T.ago(Date.now() / 1000 - 7200) + '")');

// Aggregate progress stays hidden until at least two files are in flight.
expectThrow('paintOverall()', () => T.paintOverall());
if (byId['#overall'].classList.contains('on')) {
  console.error('FAIL: overall progress shown with an empty queue');
  process.exit(1);
}
const fakeJob = (n, done) => ({
  cancelled: false, totalBytes: n, file: {size: n},
  phase: done ? 'done' : 'init', error: null,
  result: done ? {size: n} : null, doneBytes() { return 0; }
});
T.jobs.set('fake-done', fakeJob(1000, true));
T.jobs.set('fake-wip', fakeJob(2000, false));
expectThrow('paintOverall()', () => T.paintOverall());
const lbl = String((byId['#ov_lbl'] || {}).textContent);
T.jobs.delete('fake-done');
T.jobs.delete('fake-wip');
if (!byId['#overall'].classList.contains('on') || !/1 of 2 files/.test(lbl)) {
  console.error('FAIL: overall progress wrong (label=' + JSON.stringify(lbl) + ')');
  process.exit(1);
}
console.log('aggregate progress: ok (' + lbl + ')');

// ---- the two transfer lists must never overlap ---------------------------
// One page serves two audiences (the server owner and whoever was sent the
// link). The queue is "uploads from this browser"; Incoming is "uploads from
// somewhere else". If a session appears in both, the owner sees their own file
// twice and it reads as a bug.
const rowCount = sel => (byId[sel].children || []).length;
const twoOthers = () => ([
  {id: 'other-1', name: 'theirs.mkv', size: 4 * 1048576, totalChunks: 4,
   receivedChunks: 2, bytes: 2 * 1048576, elapsed: 12, idle: 1},
  {id: 'other-2', name: 'theirs2.pdf', size: 900, totalChunks: 1,
   receivedChunks: 1, bytes: 900, elapsed: 40, idle: 9},
]);
expectThrow('renderTransfers()', () => T.renderTransfers(twoOthers()));
if (rowCount('#xfers') !== 2) {
  console.error('FAIL: expected 2 incoming rows, got ' + rowCount('#xfers'));
  process.exit(1);
}

// Claim one as ours, exactly as a real upload would. It must leave Incoming
// entirely rather than being rendered with a "yours" marker.
const fake = fakeJob(4194304, false);
fake.uid = 'other-1';
T.jobs.set('local', fake);
T.renderTransfers(twoOthers());
if (rowCount('#xfers') !== 1) {
  console.error('FAIL: own session still listed in Incoming (' + rowCount('#xfers') + ' rows)');
  process.exit(1);
}
if (!/other devices/.test(String((byId['#xferhint'] || {}).textContent))) {
  console.error('FAIL: Incoming hint does not say who the rows belong to');
  process.exit(1);
}
// Only our own uploads left -> the section disappears entirely. Keep the job
// registered, otherwise other-1 is no longer "ours" and should reappear.
T.renderTransfers([
  {id: 'other-1', name: 'theirs.mkv', size: 4194304, totalChunks: 4,
   receivedChunks: 3, bytes: 3 * 1048576, elapsed: 20, idle: 0},
]);
T.jobs.delete('local');
if (rowCount('#xfers') !== 0 || !byId['#secin'].classList.contains('idle')) {
  console.error('FAIL: Incoming not hidden when only our own uploads remain');
  process.exit(1);
}
console.log('role split: ok (queue = this device, Incoming = elsewhere)');

// ---- drive a real upload ------------------------------------------------
let payload;
if (REAL) {
  payload = REAL.buf;
  console.log('uploading real file: ' + REAL.name + ' (' + payload.length + ' bytes)');
} else {
  payload = Buffer.alloc(SIZE);
  for (let i = 0; i < SIZE; i++) payload[i] = (i * 31 + (i >> 8)) & 0xff;
}
const file = new FakeFile(payload, REAL ? REAL.name : FILENAME);
CURRENT_FILE = file;

const done = new Promise((resolve, reject) => {
  const iv = setInterval(() => {
    const j = [...T.jobs.values()][0];
    if (!j) return reject(new Error('no job created'));
    if (j.phase === 'done') { clearInterval(iv); resolve(j); }
    if (j.error) { clearInterval(iv); reject(new Error(j.error)); }
  }, 60);
  setTimeout(() => { clearInterval(iv); reject(new Error('timeout')); }, 60000);
});

T.addFiles([file]);

done.then(async job => {
  const crypto = require('crypto');
  const want = crypto.createHash('sha256').update(payload).digest('hex');
  const got = job.result.sha256;
  console.log('server sha256 :', got);
  console.log('expected      :', want);
  if (got !== want) { console.error('FAIL sha mismatch'); process.exit(1); }
  if (job.result.size !== payload.length) { console.error('FAIL size', job.result.size, payload.length); process.exit(1); }
  // fetch it back through the download path
  const res = await fetch(BASE + job.result.url, {headers: {'X-Upload-Key': KEY}});
  if (res.status !== 200) { console.error('FAIL download status', res.status); process.exit(1); }
  const back = Buffer.from(await res.arrayBuffer());
  if (Buffer.compare(back, payload) !== 0) { console.error('FAIL download differs'); process.exit(1); }
  console.log('roundtrip bytes identical: yes (' + back.length + ')');
  console.log('RESULT: PASS');
  process.exit(0);
}).catch(e => { console.error('RESULT: FAIL -', e.message); process.exit(1); });
}