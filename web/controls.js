// Controls tab: quick actions, sliders, radios, stream, tools and power,
// rendered from the server's @command registry (unknown commands get a
// generic row).
import { api, run, url, h, ico, toast, haptic, sheet, choose, Slider, toggle, twoTap, INFO, fmtBytes, fmtDuration,
         copyToDevice } from './app.js';
import { hydrateIcons } from './icons.js';
import { streamSettings } from './remote.js';

const root = document.getElementById('controls');
let cmds = {}, state = {}, pending = {}, stats = {}, active = false, statsT = 0, tickT = 0;
const clockSkew = INFO.time ? INFO.time - Date.now() / 1000 : 0;
// Soft per-icon tints (mixed toward the text colour in CSS).
const TINT = { rocket: 'var(--orange)', link: 'var(--tint)', upload: 'var(--green)', folder: 'var(--teal)',
  clipboard: 'var(--purple)', cpu: 'var(--pink)', restart: 'var(--orange)', snow: 'var(--teal)',
  wifi: 'var(--tint)', bluetooth: 'var(--indigo)', power: 'var(--red)' };
const KNOWN = new Set(['sleep', 'lock', 'monitor', 'screenshot', 'play', 'prev', 'next', 'mute', 'volume', 'brightness',
  'wifi', 'bluetooth', 'launch', 'sendlink', 'sendfile', 'restart', 'shutdown', 'hibernate', 'signout']);

async function load() {
  try {
    [cmds, { state, pending }] = await Promise.all([api('/api/commands'), api('/api/state')]);
  } catch (e) { toast(e.message, { err: true }); }
  render();
}

async function act(name, args = {}, okMsg) {
  try {
    const r = await run(name, args);
    if (okMsg !== null) toast(okMsg || niceStatus(r), { ic: 'check', ms: 1400 });
    if (r && r.at) { pending[name] = r.at; renderBanner(); }
    return r;
  } catch (e) { toast(e.message, { err: true }); throw e; }
}
const niceStatus = r => (r && r.status ? r.status.replace(/_/g, ' ') : 'Done').replace(/^\w/, c => c.toUpperCase());

// ------------------------------------------------------------ pieces ---
async function busy(el, fn) {
  el.classList.add('busy');
  try { await fn(el); } catch {} finally { el.classList.remove('busy'); }
}
function tile(icn, label, onclick, on = false) {
  const t = h('button', { class: 'tile' + (on ? ' on' : ''), onclick: () => { haptic(8); busy(t, onclick); } },
    ico(icn), h('span', { 'data-label': '' }, label));
  return t;
}
/** A consequential tile: first tap arms it, the second acts; hold for extras. */
function strongTile(icn, label, onclick, onLong) {
  const t = h('button', { class: 'tile' }, ico(icn), h('span', { 'data-label': '' }, label));
  return twoTap(t, () => busy(t, onclick), { confirm: 'Tap again', onLong });
}
function row(icn, title, detail, onclick, { danger = false, right = null, strong = false, onLong = null } = {}) {
  const r = h(onclick ? 'button' : 'div', { class: 'row' + (danger ? ' danger' : ''), style: TINT[icn] ? `--ic:${TINT[icn]}` : null },
    h('span', { class: 'ric' }, ico(icn)),
    h('div', { class: 't' }, h('div', {}, title), detail ? h('div', { class: 'd' }, detail) : null),
    strong ? h('span', { class: 'arm-pill' }, 'Tap again') : null,
    right || (onclick ? ico('chev', 'chev') : null));
  if (strong) return twoTap(r, onclick, { onLong });
  if (onclick) r.addEventListener('click', () => { haptic(5); onclick(); });
  return r;
}

// Live slider: at most one request in flight, the newest value always wins.
function liveSlider(name, ic, label, value) {
  let inflight = false, next = null;
  const send = async v => {
    if (inflight) { next = v; return; }
    inflight = true;
    try { await run(name, { level: Math.round(v) }); } catch (e) { toast(e.message, { err: true }); }
    inflight = false;
    if (next !== null) { const n = next; next = null; send(n); }
  };
  return new Slider({ ic, label, value, onInput: send }).el;
}

function whenLabel(s) { return s < 60 ? `in ${s}s` : `in ${Math.round(s / 60)} min`; }
/** Press-and-hold on a power action: run it later, or forcefully. */
async function timers(name, verb, { force = false } = {}) {
  const opts = [60, 300, 900, 1800, 3600].map(s => ({ label: `${verb} ${whenLabel(s)}`, value: s }));
  if (force) opts.push({ label: `Force ${verb.toLowerCase()} now (closes apps)`, value: 'force', red: true });
  const v = await choose(verb, opts, force ? 'Unsaved work in open apps may be lost.' : 'You can cancel it from here.');
  if (v === null) return;
  await act(name, v === 'force' ? { force: true, seconds: 0 } : { seconds: v }, v === 'force' ? `${verb}…` : `${verb} ${whenLabel(v)}`);
}

// -------------------------------------------------------------- render ---
function render() {
  const s = state;
  const vol = s.volume?.level ?? 50, bright = s.brightness?.level;
  const muted = !!s.mute?.on;
  const tiles = h('div', { class: 'tiles' },
    strongTile('moon', 'Sleep', () => act('sleep', {}, 'Sleeping…'), () => timers('sleep', 'Sleep')),
    strongTile('lock', 'Lock', () => act('lock', {}, 'Locked')),
    tile('display', 'Screen off', () => act('monitor', { on: false }, 'Screen off — any input wakes it')),
    tile('camera', 'Screenshot', () => screenshotSheet()),
    tile('prev', 'Previous', () => act('prev', {}, null)),
    tile('playpause', 'Play/Pause', () => act('play', {}, null)),
    tile('next', 'Next', () => act('next', {}, null)),
    tile(muted ? 'mute' : 'volume', muted ? 'Unmute' : 'Mute', async t => {
      const now = !(state.mute?.on);
      await act('mute', { on: now }, null);
      state.mute = { on: now };
      t.classList.toggle('on', now);
      t.querySelector('[data-label]').textContent = now ? 'Unmute' : 'Mute';
      t.firstChild.replaceWith(ico(now ? 'mute' : 'volume'));
      hydrateIcons(t);
    }, muted));
  const radios = h('div', { class: 'card' },
    radioRow('wifi', 'wifi', 'Wi-Fi', 'Turning Wi-Fi off can disconnect this remote.'),
    radioRow('bluetooth', 'bluetooth', 'Bluetooth'));
  const tools = h('div', { class: 'card' },
    row('rocket', 'Open app', 'Start anything on the PC', launchSheet),
    row('link', 'Open link', 'In the PC’s browser', linkSheet),
    row('upload', 'Send files to PC', 'Saved to Downloads', uploadPick),
    row('folder', 'Get files from PC', 'Browse & download', () => filesSheet()),
    row('clipboard', 'Clipboard', 'Sync text between devices', clipboardSheet),
    row('cpu', 'Running apps', 'Memory use · end tasks', processesSheet));
  const power = h('div', { class: 'card' },
    row('restart', 'Restart', 'Hold for a timer', () => act('restart', {}, 'Restarting…'),
        { strong: true, onLong: () => timers('restart', 'Restart', { force: true }) }),
    row('power', 'Shut down', 'Hold for a timer', () => act('shutdown', {}, 'Shutting down…'),
        { danger: true, strong: true, onLong: () => timers('shutdown', 'Shut down', { force: true }) }),
    row('snow', 'Hibernate', null, () => act('hibernate', {}, 'Hibernating…'), { strong: true }),
    row('logout', 'Sign out', 'Open apps will close', () => act('signout', {}, 'Signing out…'), { strong: true }));
  const extra = Object.entries(cmds).filter(([n, m]) => !m.hide && !KNOWN.has(n));
  root.replaceChildren(h('div', {},
    h('div', { class: 'ctl-head' }, h('div', {}, h('h1', {}, INFO.host || 'PC'), h('div', { class: 'sub', id: 'ctl-stats' }))),
    h('div', { id: 'ctl-banner' }),
    tiles,
    h('div', { class: 'card pad-x' },
      liveSlider('volume', muted ? 'mute' : 'volume', 'Volume', vol),
      bright == null ? h('div', { class: 'slider', style: 'opacity:.5', title: s.brightness?.error || '' },
        h('div', { class: 'lbl' }, ico('sun'), h('span', {}, 'Brightness unavailable'))) : liveSlider('brightness', 'sun', 'Brightness', bright)),
    h('div', { class: 'group-title' }, 'Connections'), radios,
    streamSettings(),
    h('div', { class: 'group-title' }, 'Tools'), tools,
    h('div', { class: 'group-title' }, 'Power'), power,
    extra.length ? h('div', { class: 'group-title' }, 'More') : null,
    extra.length ? h('div', { class: 'card' }, extra.map(([n, m]) => row(m.icon || 'bolt', m.title, m.description,
      () => genericCommand(n, m), { strong: m.confirm && !m.params.length, danger: m.danger }))) : null,
    footer()));
  hydrateIcons(root);
  renderStats();
  renderBanner();
}

function radioRow(name, icn, title, warnOff) {
  const known = state[name] && 'on' in state[name];
  const sw = toggle(known ? state[name].on : false, async on => {
    if (!on && warnOff && !(await choose(title, [{ label: `Turn ${title} off`, value: 1, red: true }], warnOff))) {
      sw.input.checked = true; return;
    }
    try { await act(name, { on }, `${title} ${on ? 'on' : 'off'}`); state[name] = { on }; }
    catch { sw.input.checked = !on; }
  });
  if (!known) { sw.classList.add('unknown'); sw.title = state[name]?.error || 'unknown'; }
  return row(icn, title, known ? null : 'Status unavailable', null, { right: sw });
}

function footer() {
  const secure = window.isSecureContext;
  const f = h('div', { class: 'foot' },
    h('div', { id: 'ctl-ping' }, 'Measuring latency…'),
    h('div', {}, secure ? 'HD stream (H.264) active · ' : 'Plain HTTP — JPEG stream · ',
      INFO.https && !secure ? h('a', { href: INFO.https }, 'Switch to HTTPS') : null,
      INFO.https ? h('span', {}, secure ? '' : ' · ', h('a', { href: '/ca.crt' }, 'Install certificate')) : null),
    h('div', {}, 'Tip: Add to Home Screen for a full-screen app.'));
  measurePing();
  return f;
}
async function measurePing() {
  try {
    await fetch(url('/ping'));
    const t = performance.now();
    await fetch(url('/ping'));
    const el = document.getElementById('ctl-ping');
    if (el) el.textContent = `Round trip ${(performance.now() - t).toFixed(1)} ms`;
  } catch {}
}

async function pollStats() {
  if (!active) return;
  try { stats = await run('stats'); renderStats(); } catch {}
  statsT = setTimeout(pollStats, 3000);
}
function renderStats() {
  const el = document.getElementById('ctl-stats');
  if (!el) return;
  const s = stats, parts = [];
  parts.push(h('span', {}, h('i', { class: 'dot ok', style: 'margin-right:5px' }), 'Online'));
  if (s.cpu != null) parts.push(h('span', {}, 'CPU ', h('b', {}, Math.round(s.cpu) + '%')));
  if (s.ram != null) parts.push(h('span', {}, 'RAM ', h('b', {}, s.ram + '%')));
  if (s.gpu != null) parts.push(h('span', {}, 'GPU ', h('b', {}, s.gpu + '%'), s.gpu_temp ? ` ${s.gpu_temp}°` : ''));
  if (s.uptime_s) parts.push(h('span', {}, 'Up ', h('b', {}, fmtDuration(s.uptime_s))));
  el.replaceChildren(...parts);
}
function renderBanner() {
  const el = document.getElementById('ctl-banner');
  if (!el) return;
  clearTimeout(tickT);
  const now = Date.now() / 1000 + clockSkew;
  const items = Object.entries(pending).filter(([, at]) => at > now - 1);
  el.replaceChildren(...items.map(([name, at]) => h('div', { class: 'banner' }, ico('moon'),
    h('span', {}, `${name[0].toUpperCase() + name.slice(1)} in `, h('b', {}, fmtDuration(at - now))),
    h('button', { onclick: async () => { haptic(8); await act('cancel', { cmd: name }, 'Cancelled'); delete pending[name]; renderBanner(); } }, 'Cancel'))));
  hydrateIcons(el);
  if (items.length) tickT = setTimeout(renderBanner, 1000);
}

// -------------------------------------------------------------- sheets ---
function screenshotSheet() {
  const img = h('img', { class: 'shot', alt: 'Screenshot' });
  const load = () => { img.src = url('/screenshot.png', { t: Date.now() }); };
  load();
  const dl = h('a', { class: 'btn', style: 'display:grid;place-items:center;text-decoration:none', download: `screenshot-${Date.now()}.png` }, 'Save');
  img.onload = () => { dl.href = img.src; };
  sheet({ title: 'Screenshot', body: h('div', { class: 'form' }, img,
    h('div', { class: 'btns' }, h('button', { class: 'btn gray', onclick: load }, 'Retake'), dl)) });
}

async function launchSheet() {
  const q = h('input', { class: 'input', placeholder: 'App name, path or URL', autocomplete: 'off', autocapitalize: 'off', enterkeyhint: 'go' });
  const list = h('div', { class: 'card' }, h('div', { class: 'empty' }, 'Loading apps…'));
  const s = sheet({ title: 'Open app', body: h('div', { class: 'form' }, q, list) });
  let apps = [];
  const go = async name => { haptic(8); try { const r = await act('launch', { target: name }, null); toast('Opened ' + r.target, { ic: 'rocket' }); s.close(); } catch {} };
  const draw = () => {
    const t = q.value.trim().toLowerCase();
    const hits = apps.filter(a => !t || a.toLowerCase().includes(t)).slice(0, 80);
    list.replaceChildren(...(hits.length ? hits.map(a => h('button', { class: 'list-row', onclick: () => go(a) }, ico('rocket', 'ic'), h('span', { class: 'nm' }, a)))
      : [h('div', { class: 'empty' }, t ? 'Press Enter to open “' + q.value.trim() + '”' : 'No apps found')]));
    hydrateIcons(list);
  };
  q.addEventListener('input', draw);
  q.addEventListener('keydown', e => { if (e.key === 'Enter' && q.value.trim()) go(q.value.trim()); });
  try { apps = (await run('apps')).apps; } catch {}
  draw();
}

function linkSheet() {
  const inp = h('input', { class: 'input', type: 'url', placeholder: 'https://…', autocomplete: 'off', autocapitalize: 'off', enterkeyhint: 'go' });
  const s = sheet({ title: 'Open link on PC', body: h('div', { class: 'form' }, inp,
    h('div', { class: 'btns' },
      h('button', { class: 'btn gray', onclick: async () => { try { inp.value = await navigator.clipboard.readText(); } catch { toast('Paste into the field instead'); } } }, 'Paste'),
      h('button', { class: 'btn', onclick: () => go() }, 'Open'))) });
  const go = async () => { if (!inp.value.trim()) return; try { await act('sendlink', { url: inp.value.trim() }, 'Opened on PC'); s.close(); } catch {} };
  inp.addEventListener('keydown', e => e.key === 'Enter' && go());
  setTimeout(() => inp.focus(), 250);
}

function uploadPick() {
  const inp = h('input', { type: 'file', multiple: true, style: 'display:none' });
  document.body.append(inp);
  inp.addEventListener('change', () => { const files = [...inp.files]; inp.remove(); if (files.length) uploadFiles(files); });
  inp.click();
}
function uploadFiles(files) {
  const bar = h('i'), label = h('div', { style: 'font-size:14px;color:var(--label2)' });
  const s = sheet({ title: 'Sending to PC', body: h('div', { class: 'form' }, label, h('div', { class: 'progress' }, bar)) });
  const total = files.reduce((a, f) => a + f.size, 0);
  let done = 0, i = 0;
  const next = () => {
    if (i >= files.length) { toast(`Saved ${files.length} file${files.length > 1 ? 's' : ''} to Downloads`, { ic: 'check' }); s.close(); return; }
    const f = files[i++];
    label.textContent = `${f.name} (${i}/${files.length}) · ${fmtBytes(total)}`;
    const x = new XMLHttpRequest();
    x.open('POST', url('/upload', { name: f.name }));
    x.upload.onprogress = e => { bar.style.width = (100 * (done + e.loaded) / Math.max(1, total)) + '%'; };
    x.onload = () => { if (x.status !== 200) { toast('Upload failed: ' + (JSON.parse(x.responseText || '{}').error || x.status), { err: true }); s.close(); return; } done += f.size; next(); };
    x.onerror = () => { toast('Upload failed', { err: true }); s.close(); };
    x.send(f);
  };
  next();
}

async function filesSheet(path = '') {
  const s = sheet({ title: 'Files', body: h('div', { class: 'empty' }, 'Loading…') });
  const open = async p => {
    let d;
    try { d = await api('/api/files?path=' + encodeURIComponent(p)); } catch (e) { toast(e.message, { err: true }); return; }
    const places = h('div', { class: 'chips', style: 'margin-bottom:10px' }, d.places.map(pl =>
      h('button', { class: 'chip' + (d.path === pl.path ? ' on' : ''), onclick: () => open(pl.path) }, pl.name)));
    const rows = d.entries.map(e => e.dir
      ? h('button', { class: 'list-row', onclick: () => open(d.path + '\\' + e.name) }, ico('folder', 'ic'), h('span', { class: 'nm' }, e.name), ico('chev', 'meta'))
      : h('a', { class: 'list-row', style: 'color:inherit;text-decoration:none', href: url('/download', { path: d.path + '\\' + e.name }), download: e.name },
          ico('file', 'ic'), h('span', { class: 'nm' }, e.name), h('span', { class: 'meta' }, fmtBytes(e.size))));
    s.setBody(h('div', {}, places,
      d.parent ? h('button', { class: 'chip', style: 'margin-bottom:10px', onclick: () => open(d.parent) }, ico('back'), 'Up') : null,
      h('div', { style: 'font-size:12px;color:var(--label2);margin:0 2px 8px;word-break:break-all' }, d.path),
      h('div', { class: 'card' }, rows.length ? rows : [h('div', { class: 'empty' }, 'Empty folder')])));
    hydrateIcons(s.el);
  };
  open(path);
}

async function clipboardSheet() {
  const pc = h('textarea', { class: 'input', readonly: true, placeholder: 'PC clipboard is empty' });
  const mine = h('textarea', { class: 'input', placeholder: 'Text to put on the PC clipboard' });
  sheet({ title: 'Clipboard', body: h('div', { class: 'form' },
    h('div', { class: 'group-title', style: 'margin:0 4px' }, 'On the PC'), pc,
    h('button', { class: 'btn', onclick: async () => toast((await copyToDevice(pc.value)) ? 'Copied to this device' : 'Copy failed', { ic: 'check' }) }, 'Copy to this device'),
    h('div', { class: 'group-title', style: 'margin:8px 4px 0' }, 'Send to the PC'), mine,
    h('div', { class: 'btns' },
      h('button', { class: 'btn gray', onclick: async () => { try { mine.value = await navigator.clipboard.readText(); } catch { toast('Paste into the field instead'); } } }, 'Paste here'),
      h('button', { class: 'btn', onclick: () => act('paste', { text: mine.value }, 'PC clipboard set') }, 'Send'))) });
  try { pc.value = (await run('copy')).text; } catch {}
}

async function processesSheet() {
  const s = sheet({ title: 'Running apps', body: h('div', { class: 'empty' }, 'Loading…') });
  const load = async () => {
    let list;
    try { list = (await run('processes', { limit: 40 })).processes; } catch (e) { toast(e.message, { err: true }); return; }
    s.setBody(h('div', { class: 'card' }, list.map(p => h('div', { class: 'list-row' },
      h('span', { class: 'nm', style: 'flex:1' }, p.name.replace(/\.exe$/i, ''), p.pids.length > 1 ? h('span', { style: 'color:var(--label3)' }, ` ×${p.pids.length}`) : null),
      h('span', { class: 'meta' }, fmtBytes(p.mem)),
      twoTap(h('button', { class: 'chip', style: 'color:var(--red);margin-left:8px' }, h('span', { 'data-label': '' }, 'End')),
        async () => { try { await act('kill', { name: p.name }, 'Ended ' + p.name); } catch {} load(); },
        { confirm: 'Confirm' })))));
  };
  load();
}

async function genericCommand(name, m) {
  const params = m.params || [];
  if (!params.length) {
    const r = await act(name, {});
    if (r && Object.keys(r).some(k => k !== 'status')) resultSheet(m.title, r);
    return;
  }
  const inputs = params.map(p => {
    const el = p.type === 'bool' ? toggle(p.default, () => {}) :
      h('input', { class: 'input', type: p.type === 'int' || p.type === 'float' ? 'number' : 'text', placeholder: p.default ?? '' });
    return [p, el];
  });
  const s = sheet({ title: m.title, body: h('div', { class: 'form' },
    h('div', { style: 'color:var(--label2);font-size:14px' }, m.description),
    inputs.map(([p, el]) => h('label', { class: 'setting', style: 'padding:0' }, h('div', { class: 't' }, p.name), el)),
    h('button', { class: 'btn', onclick: async () => {
      const args = {};
      for (const [p, el] of inputs) {
        if (p.type === 'bool') args[p.name] = el.input.checked;
        else if (el.value !== '') args[p.name] = el.value;
      }
      const r = await act(name, args);
      s.close();
      if (r && Object.keys(r).some(k => k !== 'status')) resultSheet(m.title, r);
    } }, 'Run')) });
}
function resultSheet(title, r) {
  sheet({ title, body: h('pre', { style: 'font:12px var(--mono);white-space:pre-wrap;margin:0' }, JSON.stringify(r, null, 2)) });
}

// ---------------------------------------------------------- lifecycle ---
let loaded = false;
export function show() {
  active = true;
  if (!loaded) { loaded = true; load(); } else api('/api/state').then(d => { state = d.state; pending = d.pending; render(); }).catch(() => {});
  clearTimeout(statsT);
  pollStats();
}
export function hide() { active = false; clearTimeout(statsT); }
