/**
 * dropzone -- client upload logic.
 *
 * Loaded as a plain <script> at the end of <body>, so every element it binds
 * to already exists. It also reads #cfg synchronously on line 1, which is why
 * the JSON blob sits immediately above this tag in index.html: the HTML parser
 * has created that node by the time this runs, but nothing after this tag yet.
 *
 * Design note: no framework, no build step, no bundler. The whole point of this
 * project is that it runs from one file with zero dependencies, and the client
 * half honours the same constraint.
 */
'use strict';

const CFG = JSON.parse(document.getElementById('cfg').textContent);
const $ = s => document.querySelector(s);
const listEl = $('#list'), dropEl = $('#drop'), picker = $('#picker');
const camfile = $('#camfile'), camimg = $('#camimg');
const filesEl = $('#files');

// ---------------------------------------------------------------- auth
function setGate(on, msg){
  const g = $('#gate');
  g.hidden = !on;
  if (msg) { const e = $('#gateerr'); e.textContent = msg; e.hidden = false; }
  else $('#gateerr').hidden = true;
}
async function authed(){
  const r = await fetch('/api/config', {credentials:'same-origin', headers:{'X-Requested-With':'dz'}});
  if (r.status === 401) return false;
  if (!r.ok) throw new Error('config ' + r.status);
  return true;
}
$('#gateform').addEventListener('submit', async e => {
  e.preventDefault();
  const key = $('#gatekey').value;
  const r = await fetch('/api/auth', {method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({key})});
  if (r.ok) { setGate(false); $('#gatekey').value=''; refreshFiles(); }
  else setGate(true, 'Incorrect key.');
});

// ---------------------------------------------------------------- state
const jobs = new Map();   // local id -> job
const XHR = 'X-Upload-Key';
let savedKey = '';
try { savedKey = localStorage.getItem('dz_key') || ''; } catch(e){}
let stored = Number(CFG.uploaded) || 0;
function headers(extra){
  const h = Object.assign({}, extra || {});
  if (savedKey) h[XHR] = savedKey;
  return h;
}
function onUnauthorized(r){ if (r.status === 401) setGate(true); }

const fmt = n => {
  if (n < 1024) return n + ' B';
  const u = ['KiB','MiB','GiB','TiB']; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return (n < 10 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
};
const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));

// ----------------------------------------------------------------
// Static info. The chips, the "how it works" wording and the limits
// table all read from what the server actually reports, so they
// describe this deployment rather than a generic guess.
// ----------------------------------------------------------------
function human(n){
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GiB';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MiB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KiB';
  return n + ' B';
}
function setChip(sel, text){
  const el = $(sel);
  el.innerHTML = '';
  const b = document.createElement('b');
  b.textContent = text;
  el.appendChild(b);
}
function paintStored(){
  const el = $('#c_stored');
  el.innerHTML = '';
  const b = document.createElement('b');
  b.textContent = human(stored);
  el.appendChild(b);
  const u = document.createElement('span');
  u.textContent = ' stored';
  el.appendChild(u);
  // Row 5 of the limits table mirrors this figure.
  const t = $('#limitsTable');
  if (t && t.children.length) t.children[4].children[1].textContent = human(stored);
}
function paintInfo(){
  setChip('#c_mode', CFG.chunked ? 'resumable' : 'single-shot');
  setChip('#c_chunk', CFG.chunked ? 'chunk ' + fmt(CFG.chunkSize) : 'one request');
  setChip('#c_max', CFG.maxFileSize ? 'max ' + fmt(CFG.maxFileSize) : 'no size limit');
  paintStored();

  $('#how_chunk').textContent = CFG.chunked ? fmt(CFG.chunkSize) : 'one';
  $('#how_par').textContent = CFG.chunked
    ? plural(Math.max(1, CFG.parallel), 'piece') + ' go'
    : 'The file goes';

  const rows = [
    ['chunk size', human(CFG.chunkSize)],
    ['parallel requests', String(Math.max(1, CFG.parallel))],
    ['per-file cap', CFG.maxFileSize ? human(CFG.maxFileSize) : 'unlimited'],
    ['directory cap', CFG.maxTotal ? human(CFG.maxTotal) : 'unlimited'],
    ['stored now', human(stored)],
    ['key required', CFG.authRequired ? 'yes' : 'NO -- running open']
  ];
  const t = $('#limitsTable');
  t.innerHTML = '';
  for (const r of rows){
    const tr = document.createElement('tr');
    const a = document.createElement('td'), b = document.createElement('td');
    a.textContent = r[0]; b.textContent = r[1];
    tr.appendChild(a); tr.appendChild(b); t.appendChild(tr);
  }
  if (CFG.repo){
    const p = $('#srcline');
    p.innerHTML = '';
    const lead = document.createElement('span');
    lead.textContent = 'Source, setup and the full list of limitations: ';
    const a = document.createElement('a');
    a.href = CFG.repo; a.textContent = CFG.repo.replace(/^https?:\/\//, ''); a.rel = 'noopener';
    p.appendChild(lead); p.appendChild(a);
  }
}

// ------------------------------------------------- files already on the server
function ago(ts){
  const s = Math.max(0, Date.now() / 1000 - (Number(ts) || 0));
  if (s < 90) return 'just now';
  if (s < 5400) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  if (s < 86400 * 45) return Math.round(s / 86400) + 'd ago';
  return new Date(ts * 1000).toISOString().slice(0, 10);
}
function renderFiles(list){
  filesEl.innerHTML = '';
  const hint = $('#fileshint');
  if (!CFG.serveDownloads){
    hint.textContent = 'downloads disabled';
    return;
  }
  if (!list.length){
    hint.textContent = 'empty';
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = 'Nothing stored yet. The first upload you make shows up here.';
    filesEl.appendChild(p);
    return;
  }
  hint.textContent = plural(list.length, 'file') + ' · newest first';
  for (const f of list.slice(0, 25)){
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.className = 'fn'; a.href = f.url; a.target = '_blank'; a.rel = 'noopener';
    a.textContent = f.name; a.title = f.name;
    const s = document.createElement('span');
    s.className = 'fs';
    s.textContent = human(f.size) + ' · ' + ago(f.mtime);
    li.appendChild(a); li.appendChild(s);
    filesEl.appendChild(li);
  }
}
async function refreshFiles(){
  if (!CFG.serveDownloads) return;
  try {
    const r = await fetch('/api/list', {credentials:'same-origin', headers:headers()});
    if (r.status === 401) return onUnauthorized(r);
    if (!r.ok) return;
    const d = await r.json();
    // NB: /api/list is capped and says nothing about in-flight chunks, so it
    // must not drive the "stored" figure. That is CFG.uploaded plus whatever
    // finished during this session.
    renderFiles(d.files || []);
  } catch(e){ /* the queue still works without the file list */ }
}

// ------------------------------------------------------------------
// Live dashboard.
//
// One poll of /api/stats drives everything below: the live strip, the
// incoming feed and the storage figures. One endpoint rather than three
// because polling them separately triples the request budget and the
// numbers would disagree with each other by a few hundred ms.
//
// Polling rather than SSE on purpose. The payload is a few hundred bytes,
// and a short poll is far more robust through a tunnel than a long-lived
// connection the proxy may hold open, buffer or reset.
//
// The server computes the transfer rate; deriving it here from successive
// polls would report our own poll interval as upload speed.
// ------------------------------------------------------------------
const XFER_POLL_BUSY = 1000;   // while something is moving
const XFER_POLL_IDLE = 4000;   // nothing in flight, just watching
const XFER_STALL_AFTER = 20;   // seconds without progress before "stalled"
const xferPrev = new Map();    // id -> {bytes, at}, for per-row ETA only
let xferTimer = null;
let lastSpeed = 0;

function rate(n){
  if (!(n > 0)) return '0 B/s';
  if (n < 1024) return Math.round(n) + ' B/s';
  const u = ['KiB/s','MiB/s','GiB/s']; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return (n < 10 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
}
function dur(s){
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.round((s % 3600) / 60) + 'm';
  return Math.floor(s / 86400) + 'd';
}
function uptime(s){
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm ' + Math.floor(s % 60) + 's';
}

// The strip is hidden entirely when nothing is happening, so a page sitting
// idle does not show a row of zeros.
function paintLive(st){
  const box = $('#live');
  const sp = Number(st.speed) || 0;
  lastSpeed = sp;
  const moving = sp > 0 || (st.transfers && st.transfers.length > 0);
  if (!moving && !box.dataset.armed){
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.dataset.armed = '1';
  box.classList.toggle('moving', sp > 0);

  const parts = [];
  if (st.activeCount) parts.push(plural(st.activeCount, 'transfer'));
  else parts.push('idle');
  $('#lv_speed').textContent = (st.activeCount ? rate(sp) : 'idle') + ' · ' + parts.join(' ');

  // storage.bytes is the TOTAL on disk (finished files + in-flight chunks) --
  // assembling a file moves bytes from one bucket to the other without
  // changing the total. Subtract before labelling, or the strip claims more
  // than the server holds.
  const total = (st.storage && st.storage.bytes) || 0;
  const partial = (st.storage && st.storage.partialBytes) || 0;
  const finished = Math.max(0, total - partial);
  const inflight = partial > 0 ? ' · ' + human(partial) + ' in flight' : '';

  // The quota bar only exists when a quota is actually configured.
  const q = $('#lv_quota');
  const quota = st.storage && st.storage.quota;
  if (quota){
    q.hidden = false;
    // Everything on disk counts against the quota, in-flight included.
    const pct = Math.max(0, Math.min(100, total / quota * 100));
    q.querySelector('i').style.transform = 'scaleX(' + (pct / 100) + ')';
    // Warn before it is a problem, not when it already is one.
    q.classList.toggle('warn', pct >= 90);
    $('#lv_stored').textContent = human(total) + ' / ' + human(quota) + inflight;
  } else {
    q.hidden = true;
    $('#lv_stored').textContent = human(finished) + ' stored' + inflight;
  }
  $('#lv_files').textContent = plural((st.storage && st.storage.files) || 0, 'file');
  $('#lv_uptime').textContent = 'up ' + uptime(st.uptime || 0);
}

function renderTransfers(list){
  const sec = $('#secin'), ul = $('#xfers'), hint = $('#xferhint');

  // There is ONE page for two audiences: whoever runs the server, and whoever
  // was sent the link. So this list is strictly "uploads from somewhere other
  // than this browser" -- anything this page is driving itself already has a
  // detailed, actionable row in the queue directly above. Showing a session
  // in both places made the owner see their own file twice and read as a bug.
  const mine = new Set([...jobs.values()].map(j => j.uid).filter(Boolean));
  const others = list.filter(t => !mine.has(t.id));

  // Never drop a session whose rate we were tracking just because it left
  // the list -- that would leak entries in xferPrev across many transfers.
  const seen = new Set();
  for (const t of others) seen.add(t.id);
  for (const id of [...xferPrev.keys()]) if (!seen.has(id)) xferPrev.delete(id);

  if (!others.length){
    sec.classList.add('idle');
    hint.textContent = '';
    ul.innerHTML = '';
    return;
  }
  sec.classList.remove('idle');
  const at = Date.now() / 1000;

  ul.innerHTML = '';
  for (const t of others){
    const pct = t.size > 0 ? Math.min(100, t.bytes / t.size * 100) : 0;
    const prev = xferPrev.get(t.id);
    let moving = true;
    if (prev){
      const dt = at - prev.at;
      // Only trust a rate over a non-trivial window, or a 1s poll boundary
      // rounding turns into a nonsense "4.2 GiB/s".
      moving = dt > 0.4 && t.bytes > prev.bytes;
    }
    xferPrev.set(t.id, {bytes: t.bytes, at: at});

    const li = document.createElement('li');
    if (t.idle > XFER_STALL_AFTER) li.classList.add('stalled');

    const top = document.createElement('div');
    top.className = 'top';
    const fn = document.createElement('span');
    fn.className = 'fn'; fn.textContent = t.name; fn.title = t.name;
    const pc = document.createElement('span');
    pc.className = 'pct';
    pc.textContent = (t.size > 0 ? Math.floor(pct) : 0) + '%';
    top.appendChild(fn); top.appendChild(pc);

    const bar = document.createElement('div');
    bar.className = 'bar';
    const fill = document.createElement('i');
    fill.style.transform = 'scaleX(' + (pct / 100) + ')';
    bar.appendChild(fill);

    const sub = document.createElement('div');
    sub.className = 'sub';
    const bits = [];
    bits.push(human(t.bytes) + (t.size > 0 ? ' / ' + human(t.size) : ''));
    if (prev && moving && at > prev.at){
      const r = (t.bytes - prev.bytes) / (at - prev.at);
      bits.push(rate(r));
      if (t.size > 0 && r > 0){
        const left = (t.size - t.bytes) / r;
        if (left < 86400) bits.push(dur(left) + ' left');
      }
    } else if (t.idle > XFER_STALL_AFTER){
      bits.push('waiting ' + dur(t.idle));
    } else {
      bits.push(dur(t.elapsed));
    }
    for (const b of bits){
      const s = document.createElement('span');
      s.textContent = b;
      sub.appendChild(s);
    }

    li.appendChild(top); li.appendChild(bar); li.appendChild(sub);
    ul.appendChild(li);
  }
  hint.textContent = plural(others.length, 'transfer') + ' from other devices';
}

async function pollTransfers(){
  try {
    const r = await fetch('/api/stats', {credentials:'same-origin', headers:headers()});
    if (r.status === 401) return;
    if (!r.ok) return;
    const d = await r.json();
    paintLive(d);
    renderTransfers(d.transfers || []);
    paintStoredFromStats(d);
  } catch(e){ /* transient; the next tick retries */ }
  finally {
    // Slow down when nothing is moving, but never stop entirely: the whole
    // point is to notice a transfer that has not started yet.
    const busy = lastSpeed > 0
      || [...jobs.values()].some(j => !j.cancelled && j.phase !== 'done' && !j.error);
    const delay = busy ? XFER_POLL_BUSY : XFER_POLL_IDLE;
    xferTimer = setTimeout(pollTransfers, delay);
  }
}
function startTransferFeed(){
  if (xferTimer) return;
  pollTransfers();
}
// Keep the header chip honest without fighting the live numbers: the server
// is the source of truth for "stored", so prefer it once we have it.
let haveStats = false;
function paintStoredFromStats(st){
  if (!st || !st.storage) return;
  haveStats = true;
  stored = Number(st.storage.bytes) || 0;
  paintStored();
}
// A hidden tab is not a useful place to spend a poll; the queue above still
// shows local progress, and the feed catches up on return.
document.addEventListener('visibilitychange', () => {
  if (document.hidden){ if (xferTimer){ clearTimeout(xferTimer); xferTimer = null; } }
  else startTransferFeed();
});

// Resume is per-session in the server's session store; the client only needs
// to remember that a session is in flight so a refresh can offer to re-attach
// by re-picking the same file (same name+size+lastModified fingerprint).
const FINGERPRINTS = 'dz_fingerprints';
function savePending(){
  try {
    const out = {};
    for (const j of jobs.values()) {
      if (j.phase === 'done' || j.error) continue;
      out[fingerprint(j.file)] = {uid: j.uid, chunkSize: j.chunkSize, total: j.total,
                                  have: [...j.have], size: j.file.size};
    }
    localStorage.setItem(FINGERPRINTS, JSON.stringify(out));
  } catch(e){}
}
function loadPending(){ try { return JSON.parse(localStorage.getItem(FINGERPRINTS) || '{}'); } catch(e){ return {}; } }
function forgetPending(f){ const p = loadPending(); delete p[f]; try { localStorage.setItem(FINGERPRINTS, JSON.stringify(p)); } catch(e){} }
function fingerprint(f){ return f.name + ':' + f.size + ':' + f.lastModified; }
function fp_all(obj){ return Object.keys(obj); }

// ---------------------------------------------------------------- upload
// Fallback for servers started with --no-chunked: one multipart POST.
async function startSimple(job){
  job.phase = 'init';
  const fd = new FormData();
  fd.append('file', job.file, job.file.name);
  await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    job.xhrs.add(xhr);
    xhr.open('POST', '/api/upload', true);
    if (savedKey) xhr.setRequestHeader(XHR, savedKey);
    xhr.upload.onprogress = e => { if (e.lengthComputable) paint(job, e.loaded / e.total * 100); };
    xhr.onload = () => {
      job.xhrs.delete(xhr);
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch(_){}
      if (xhr.status === 401) { onUnauthorized(xhr); job.paused = true; return reject(new Error('unauthorized')); }
      if (xhr.status >= 200 && xhr.status < 300){ job.result = d; job.phase = 'done'; paint(job, 100); resolve(); }
      else reject(new Error(d.error || ('HTTP ' + xhr.status)));
    };
    xhr.onerror = () => { job.xhrs.delete(xhr); reject(new Error('network error')); };
    xhr.onabort = () => { job.xhrs.delete(xhr); reject(new Error('aborted')); };
    xhr.send(fd);
  });
}

async function start(job){
  job.phase = 'init';
  if (!CFG.chunked) return startSimple(job);

  // If a previous attempt at this exact file left a session on the server,
  // ask the server which chunks it still needs before re-sending anything.
  const fp = fingerprint(job.file);
  const prior = loadPending()[fp];
  if (prior && prior.uid) {
    try {
      const r = await fetch('/api/chunk/' + prior.uid, {credentials:'same-origin', headers:headers()});
      if (r.ok){
        const d = await r.json();
        if (d.chunkSize === prior.chunkSize && d.totalChunks === prior.total){
          job.uid = prior.uid;
          job.chunkSize = d.chunkSize;
          job.total = d.totalChunks;
          job.have = new Set(d.received || []);
          savePending();
          return pump(job);
        }
      } else {
        forgetPending(fp);  // session expired server-side; start clean
      }
    } catch(e){}
  }

  const r = await fetch('/api/init', {method:'POST', credentials:'same-origin',
    headers: headers({'Content-Type':'application/json'}),
    body: JSON.stringify({name: job.file.name, size: job.file.size, chunkSize: CFG.chunkSize})});
  if (r.status === 401) return onUnauthorized(r);
  if (!r.ok) throw new Error('init failed (' + r.status + ')');
  const data = await r.json();
  job.uid = data.uploadId;
  job.chunkSize = data.chunkSize;
  job.total = data.totalChunks;
  job.have = new Set(data.received || []);
  job.totalBytes = job.file.size || 0;
  savePending();
  await pump(job);
}

async function pump(job){
  if (job.cancelled || job.paused) return;
  const todo = [];
  for (let i = 0; i < job.total; i++) if (!job.have.has(i)) todo.push(i);
  if (!todo.length) return finish(job);

  // One shared cursor across N lanes. Each lane claims the next index and
  // uploads it; any lane rejecting aborts the whole file.
  let cursor = 0;
  const lane = async () => {
    for (;;){
      if (job.cancelled || job.paused) return;
      const i = cursor++;
      if (i >= todo.length) return;
      await sendChunk(job, i);
    }
  };
  const lanes = Math.max(1, CFG.parallel);
  await Promise.all(Array.from({length: lanes}, lane));

  if (job.cancelled || job.paused) return;
  if (todo.every(i => job.have.has(i))) return finish(job);
  // A lane rejected but the rest were fine: retry whatever is still missing.
  await pump(job);
}

function sendChunk(job, idx){
  // Each chunk is retried independently. On a slow mobile link a single
  // request can be cancelled in flight (Cloudflare's ~100s timeout, network
  // handover, screen lock). Retrying just that chunk is what turns a failed
  // upload into a slightly slower successful one.
  //
  // 8 attempts, not 5: failures here are probabilistic rather than
  // deterministic, so the chance of a chunk surviving N tries falls off as
  // p^N. With a bad link ~1 in 3 requests dies, and a 200-chunk file then
  // needs enough attempts that the product over all its chunks stays tiny.
  // Retrying is safe: the server only publishes a chunk after the full body
  // has been received, so a severed attempt leaves nothing behind.
  const MAX_TRIES = 8;
  return new Promise((resolve, reject) => {
    const start = idx * job.chunkSize;
    const blob = job.file.slice(start, Math.min(start + job.chunkSize, job.file.size));
    const xhr = new XMLHttpRequest();
    job.xhrs.add(xhr);
    xhr.open('PUT', '/api/chunk/' + job.uid + '/' + idx, true);
    if (savedKey) xhr.setRequestHeader(XHR, savedKey);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    // Do not let one chunk hang forever: abort so the retry can start.
    xhr.timeout = CFG.requestTimeout || 120000;
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const nowDone = job.doneBytes() - (job.inflight.get(idx) || 0) + e.loaded;
      job.inflight.set(idx, e.loaded);
      paint(job, Math.min(100, nowDone / job.totalBytes * 100));
    };
    const cleanup = () => { job.inflight.delete(idx); job.xhrs.delete(xhr); };

    xhr.onload = () => {
      cleanup();
      if (xhr.status === 401) { onUnauthorized(xhr); job.paused = true; return reject(new Error('unauthorized')); }
      if (xhr.status >= 200 && xhr.status < 300){
        job.have.add(idx);
        job.retries = 0;
        paint(job, job.doneBytes() / job.totalBytes * 100);
        return resolve();
      }
      // 408/429/5xx are transient: worth another go. 4xx (other than the
      // above) means this chunk will never be accepted, so give up now.
      const transient = xhr.status === 408 || xhr.status === 429 || xhr.status >= 500;
      let detail = '';
      try { detail = (JSON.parse(xhr.responseText) || {}).error || ''; } catch(e){}
      retryOrFail(job, idx, transient,
                  'chunk ' + idx + ' -> HTTP ' + xhr.status + (detail ? ' (' + detail + ')' : ''),
                  resolve, reject, MAX_TRIES, cleanup, xhr);
    };
    xhr.onerror = () => { cleanup(); retryOrFail(job, idx, true, 'network error on chunk ' + idx, resolve, reject, MAX_TRIES, cleanup, xhr); };
    xhr.ontimeout = () => { cleanup(); retryOrFail(job, idx, true, 'chunk ' + idx + ' timed out', resolve, reject, MAX_TRIES, cleanup, xhr); };
    xhr.onabort = () => { cleanup(); reject(new Error('aborted')); };
    xhr.send(blob);
  });
}

function retryOrFail(job, idx, transient, msg, resolve, reject, maxTries, cleanup, xhr){
  job.tries = job.tries || {};
  const n = (job.tries[idx] = (job.tries[idx] || 0) + 1);
  if (!transient || n >= maxTries || job.cancelled || job.paused){
    delete job.tries[idx];
    return reject(new Error(n >= maxTries ? msg + ' (gave up after ' + n + ' tries)' : msg));
  }
  // Exponential backoff with jitter, so N parallel lanes don't resynchronise
  // and hammer the server in lockstep.
  const wait = Math.min(8000, 500 * Math.pow(2, n - 1)) + Math.random() * 400;
  paint(job, 0, 'retrying chunk ' + idx + ' in ' + (wait/1000).toFixed(1) + 's (' + n + '/' + maxTries + ')');
  setTimeout(() => {
    if (job.cancelled || job.paused) return reject(new Error('aborted'));
    job.xhrs.add(xhr);
    xhr.open('PUT', '/api/chunk/' + job.uid + '/' + idx, true);
    if (savedKey) xhr.setRequestHeader(XHR, savedKey);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.send(job.file.slice(idx * job.chunkSize, Math.min((idx + 1) * job.chunkSize, job.file.size)));
  }, wait);
}

async function finish(job){
  job.phase = 'finish';
  paint(job, 100, 'verifying');
  const r = await fetch('/api/finish', {method:'POST', credentials:'same-origin',
    headers: headers({'Content-Type':'application/json'}),
    body: JSON.stringify({uploadId: job.uid, name: job.file.name, size: job.file.size})});
  if (r.status === 401) return onUnauthorized(r);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || ('finish failed (' + r.status + ')'));
  job.result = data;
  job.phase = 'done';
  forgetPending(fingerprint(job.file));
  // Only track locally until the server starts reporting; after that its
  // figure wins and a local guess would drift on every retry or resume.
  if (!haveStats) stored += Number(data.size) || 0;
  paintStored();
  paintOverall();
  refreshFiles();
  // Pull stats immediately so the storage figure and incoming feed update
  // now instead of on the next poll tick.
  pollTransfers();
  paint(job, 100, '');
}

const State = {
  init(){ return 'connecting…'; },
  finish(){ return 'verifying…'; },
  done(){ return ''; },
  paused(){ return 'paused'; },
  err(job){ return job.error || 'failed'; },
  up(job){ return fmt(job.doneBytes()) + ' / ' + fmt(job.totalBytes); }
};

// Aggregate line for the whole queue. Only worth the pixels once more than one
// file is in flight; a single upload already has its own bar.
function paintOverall(){
  const box = $('#overall');
  const all = [...jobs.values()].filter(j => !j.cancelled);
  // Step the ambient backdrop back while anything is in flight, so the
  // progress bars are the loudest thing on screen. Guarded because the
  // test harness's document shim has no body.
  if (document.body) document.body.classList.toggle('busy', all.length > 0);
  // Label the queue only once it holds something: "Uploading from this
  // device" is the answer to "why is there a second list below?".
  const ql = $('#qlabel');
  if (ql) ql.hidden = all.length === 0;
  if (all.length < 2){ box.classList.remove('on'); $('#ov_act').hidden = true; return; }
  let totalB = 0, doneB = 0, doneN = 0, bad = 0;
  for (const j of all){
    const whole = j.totalBytes || j.file.size || 0;
    totalB += whole;
    if (j.phase === 'done'){ doneB += (j.result && j.result.size) || whole; doneN++; }
    else if (j.error){ bad++; }
    else doneB += j.doneBytes();
  }
  const pct = totalB ? Math.min(100, doneB / totalB * 100) : 0;
  box.classList.add('on');
  box.querySelector('.bar>i').style.transform = 'scaleX(' + (pct / 100) + ')';
  $('#ov_lbl').textContent = doneN + ' of ' + all.length + ' files complete'
    + (bad ? ' · ' + plural(bad, 'failed') : '');
  $('#ov_sub').textContent = human(doneB) + ' / ' + human(totalB);
  $('#ov_act').hidden = !bad;
}

function paint(job, pct, status){
  const li = job.li;
  // scaleX, not width: the bar then animates on the compositor and a progress
  // tick never costs a layout pass.
  li.querySelector('.bar>i').style.transform =
    'scaleX(' + (Math.max(0, Math.min(100, pct)) / 100) + ')';
  let s = status;
  if (!s) {
    if (job.phase === 'done') s = '';
    else if (job.bgNote) s = job.bgNote;
    else if (job.paused) s = 'paused — tap resume to continue';
    else if (job.error) s = job.error;
    else s = State.up(job);
  }
  li.querySelector('.st').textContent = s;
  li.classList.toggle('done', job.phase === 'done');
  li.classList.toggle('err', !!job.error);
  if (job.phase === 'done') {
    const acts = li.querySelector('.act');
    if (job.result && !acts.dataset.filled){
      acts.dataset.filled = '1';
      const a = document.createElement('a');
      a.href = job.result.url; a.textContent = job.result.name; a.target = '_blank';
      a.rel = 'noopener';
      const copy = document.createElement('button');
      copy.textContent = 'copy link';
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(new URL(job.result.url, location.href).href); copy.textContent = 'copied'; }
        catch(e){ copy.textContent = 'press ⌘C'; }
        setTimeout(() => copy.textContent = 'copy link', 1400);
      };
      acts.appendChild(a); acts.appendChild(copy);
      // First bytes of the digest, so it can be eyeballed against `sha256sum`
      // on the other end without opening the link.
      if (job.result.sha256){
        const h = document.createElement('span');
        h.className = 'sha';
        h.textContent = 'sha256 ' + job.result.sha256.slice(0, 12);
        acts.appendChild(h);
      }
    }
  }
  paintOverall();
}

function makeJob(file){
  const job = {
    id: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()+Math.random())),
    file, uid:null, chunkSize:CFG.chunkSize, total:0, have:new Set(),
    totalBytes:file.size, inflight:new Map(), xhrs:new Set(),
    phase:'init', paused:false, cancelled:false, error:null, result:null,
    doneBytes(){ let n = 0; for (const i of this.have) n += Math.min(this.chunkSize, Math.max(0, this.file.size - i*this.chunkSize)); return n; }
  };
  const li = document.createElement('li');
  li.innerHTML = '<div class="row"><span class="nm"></span><span class="sz"></span></div>' +
    '<div class="bar"><i></i></div><div class="row" style="margin-top:7px"><span class="st"></span></div>' +
    '<div class="act"></div>';
  li.querySelector('.nm').textContent = file.name;
  li.querySelector('.nm').title = file.name;
  li.querySelector('.sz').textContent = fmt(file.size);
  job.li = li;
  listEl.prepend(li);
  return job;
}

async function run(job){
  try {
    await start(job);
  } catch (e){
    if (job.cancelled || job.paused) return;
    job.error = (e && e.message) || String(e);
    job.phase = 'err';
    savePending();  // keep the session so "pause -> resume" can recover
    paint(job, 0);
  }
}

function wire(job){
  const acts = job.li.querySelector('.act');
  const pause = document.createElement('button');
  pause.textContent = 'pause';
  pause.onclick = () => {
    if (job.paused){
      // Resume re-attaches to the existing server-side session, so already
      // received chunks are never re-sent.
      job.paused = false; job.error = null; job.bgNote = null;
      pause.textContent = 'pause';
      if (job.phase === 'err') run(job); else pump(job).catch(e => { job.error = e.message; paint(job, 0); });
    } else {
      job.paused = true;
      for (const x of job.xhrs) x.abort();
      job.xhrs.clear();
      paint(job, 0);
    }
  };
  const cancel = document.createElement('button');
  cancel.textContent = 'cancel';
  cancel.onclick = () => {
    job.cancelled = true; job.paused = true;
    for (const x of job.xhrs) x.abort(); job.xhrs.clear();
    if (job.uid) fetch('/api/chunk/' + job.uid, {method:'DELETE', credentials:'same-origin', headers:headers()}).catch(()=>{});
    job.li.remove(); jobs.delete(job.id); savePending(); paintOverall();
  };
  acts.appendChild(pause); acts.appendChild(cancel);
}

function addFiles(files){
  for (const f of files){
    if (f.size === 0){
      // A zero-byte File from an iOS camera roll entry or an empty <input>
      // can never produce a chunk, so reject it up front rather than
      // spinning forever on a zero-length upload.
      const j = makeJob(f); j.error = 'empty file'; j.phase = 'err'; paint(j, 0); continue;
    }
    if (CFG.maxFileSize && f.size > CFG.maxFileSize){
      const j = makeJob(f); j.error = 'too large (limit ' + fmt(CFG.maxFileSize) + ')'; j.phase = 'err'; paint(j, 0); continue;
    }
    const j = makeJob(f);
    jobs.set(j.id, j);
    wire(j);
    paint(j, 0);
    run(j);
  }
}

// ---------------------------------------------------------------- inputs
// Desktop: drag & drop onto the zone.
// Touch:   no drag & drop exists on mobile, so the two buttons are the
//          primary path there. `#cam` (accept image/*,video/* + capture) opens
//          the OS picker which offers Camera / Photo Library on both iOS and
//          Android; a separate accept="image/*" input gives a cleaner
//          camera-first experience for stills on Android.
const isTouch = matchMedia('(hover:none)').matches || 'ontouchstart' in window;

$('#pick').addEventListener('click', e => { e.stopPropagation(); picker.click(); });
$('#cam').addEventListener('click', e => { e.stopPropagation(); camfile.click(); });
camfile.addEventListener('change', () => { if (camfile.files.length) addFiles(camfile.files); camfile.value = ''; });
picker.addEventListener('change', () => { if (picker.files.length) addFiles(picker.files); picker.value = ''; });
$('#retryall').addEventListener('click', () => {
  for (const j of jobs.values()){
    if (j.error && !j.cancelled){ j.error = null; j.phase = 'init'; paint(j, 0); run(j); }
  }
});

if (isTouch){
  $('#droptitle').textContent = 'Send a file';
  $('#dropsub').textContent = 'resumable · works on slow connections';
  $('#drop .glyph svg').innerHTML =
    '<path d="M12 20V8m0 0L7.5 12.5M12 8l4.5 4.5"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>';
  // Hide the camera shortcut where it does nothing (desktop with no capture).
  if (!('capture' in document.createElement('input'))) $('#cam').hidden = true;
}

let dragDepth = 0;
dropEl.addEventListener('dragenter', e => {
  e.preventDefault();
  // dragenter/dragleave also fire for child elements. Counting depth keeps the
  // highlight from flickering as the pointer crosses them.
  dragDepth++; dropEl.classList.add('hot');
});
dropEl.addEventListener('dragover', e => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
dropEl.addEventListener('dragleave', e => {
  e.preventDefault();
  if (--dragDepth <= 0){ dragDepth = 0; dropEl.classList.remove('hot'); }
});
dropEl.addEventListener('drop', e => {
  e.preventDefault(); dragDepth = 0; dropEl.classList.remove('hot');
  const fs = e.dataTransfer && e.dataTransfer.files;
  if (fs && fs.length) addFiles(fs);
});
// Only the zone itself opens the picker, not the buttons inside it.
dropEl.addEventListener('click', e => { if (e.target === dropEl || e.target.closest('.glyph')) picker.click(); });
// Without this, dropping anywhere on the page navigates to the dropped file.
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => e.preventDefault());

// Paste: photos and screenshots pasted from the clipboard (mobile + desktop).
window.addEventListener('paste', e => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  const out = [];
  for (const it of items){
    if (it.kind === 'file'){
      const f = it.getAsFile();
      if (f) out.push(f);
    }
  }
  if (out.length){ e.preventDefault(); addFiles(out); }
});

// ------------------------------------------------- backgrounding / leaving
// This is the single biggest mobile-upload failure mode. When an iOS/Android
// browser is backgrounded it freezes timers and often stalls in-flight
// XHRs, so a large upload that the user left alone can sit at 40% forever.
// Detect it and tell the user rather than letting it hang.
let warnedBg = false;
document.addEventListener('visibilitychange', () => {
  const active = [...jobs.values()].filter(j => !j.cancelled && !j.paused && j.phase !== 'done' && !j.error);
  if (document.hidden){
    if (active.length && !warnedBg){
      warnedBg = true;
      for (const j of active) j.bgNote = 'paused while you were away — tap resume';
      $('#conn').textContent = 'hidden: uploads may pause until you return';
    }
  } else {
    if (warnedBg){
      warnedBg = false;
      $('#conn').textContent = CFG.chunked ? 'chunked · resumable' : 'single-shot';
      // The socket may have died while hidden: ask the server which chunks
      // actually landed rather than trusting the local counters.
      for (const j of active) resync(j);
    }
  }
});

// Reconcile one job against the server's idea of what it holds.
async function resync(job){
  if (!job.uid || !CFG.chunked) return;
  try {
    const r = await fetch('/api/chunk/' + job.uid, {credentials:'same-origin', headers:headers()});
    if (!r.ok) return;
    const d = await r.json();
    if (d.chunkSize !== job.chunkSize || d.totalChunks !== job.total) return;
    job.have = new Set(d.received || []);
    job.bgNote = null;
    paint(job, job.doneBytes() / (job.totalBytes || 1) * 100);
    if (!job.paused) pump(job).catch(e => { job.error = e.message; paint(job, 0); });
  } catch(e){}
}

// Warn before leaving with work in flight.
window.addEventListener('beforeunload', e => {
  const active = [...jobs.values()].some(j => !j.cancelled && !j.error && j.phase !== 'done');
  if (active){ e.preventDefault(); e.returnValue = ''; }
});

// Keep the screen awake during big transfers where the browser allows it.
try { if ('wakeLock' in navigator) navigator.wakeLock.request('screen').catch(()=>{}); } catch(e){}

// ---------------------------------------------------------------- boot
(async () => {
  paintInfo();
  $('#limits').textContent = 'chunk ' + fmt(CFG.chunkSize) + ' · '
    + (CFG.maxFileSize ? 'max ' + fmt(CFG.maxFileSize) : 'no size limit');
  $('#conn').textContent = CFG.chunked ? 'chunked · resumable' : 'single-shot';
  try { await authed(); } catch(e){ setGate(true, 'Cannot reach server.'); return; }
  refreshFiles();
  startTransferFeed();

  // On reload, surface any session the server is still holding. The browser
  // forgets the File object itself, so the user has to re-pick the same file;
  // start() then matches on name+size+mtime and resumes without re-sending the
  // chunks the server already has.
  const pending = loadPending();
  const n = Object.keys(pending).length;
  if (n){
    const li = document.createElement('li');
    li.style.borderStyle = 'dashed';
    const r = document.createElement('div');
    r.className = 'row';
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = n + ' unfinished ' + (n === 1 ? 'upload' : 'uploads') + ' found';
    const b = document.createElement('button');
    b.textContent = 'discard';
    b.onclick = () => {
      for (const fp of Object.keys(pending)){
        if (pending[fp].uid){
          fetch('/api/chunk/' + pending[fp].uid, {method:'DELETE', credentials:'same-origin', headers:headers()}).catch(()=>{});
        }
      }
      forgetPending(fp_all(pending));
      li.remove();
    };
    r.appendChild(nm); r.appendChild(b);
    const tip = document.createElement('div');
    tip.style.cssText = 'color:var(--dim);font-size:12px;margin-top:6px';
    tip.textContent = 'Re-choose the same file to continue where it stopped.';
    li.appendChild(r); li.appendChild(tip);
    listEl.appendChild(li);
  }
})();