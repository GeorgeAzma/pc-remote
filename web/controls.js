// Controls tab: quick actions, sliders, radios, stream, tools and power,
// rendered from the server's @command registry (unknown commands get a
// generic row).
import { api, run, url, wsUrl, h, ico, toast, haptic, sheet, choose, Slider, toggle, twoTap, INFO, fmtBytes, fmtDuration,
         copyToDevice, tabSwitch } from './app.js';
import { hydrateIcons } from './icons.js';
import { resolvePlan, METHODS } from './remote.js';

const root = document.getElementById('controls');
let cmds = {}, state = {}, pending = {}, stats = {}, active = false, statsT = 0, tickT = 0, rtt = null, about = null;
// Chrome/Edge/Android offer a real "install" prompt; keep it for the Install as app page.
let installPrompt = null;
addEventListener('beforeinstallprompt', e => { e.preventDefault(); installPrompt = e; });
const clockSkew = INFO.time ? INFO.time - Date.now() / 1000 : 0;
// Soft per-icon tints (mixed toward the text colour in CSS).
const TINT = { rocket: 'var(--orange)', link: 'var(--tint)', upload: 'var(--green)', folder: 'var(--teal)',
  clipboard: 'var(--purple)', cpu: 'var(--pink)', restart: 'var(--orange)', snow: 'var(--teal)',
  wifi: 'var(--tint)', bluetooth: 'var(--indigo)', power: 'var(--red)', info: 'var(--gray)', shield: 'var(--green)',
  download: 'var(--tint)' };
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
    row('cpu', 'Running apps', 'Memory use · end tasks', processesSheet),
    row('pulse', 'System monitor', 'Every core, memory, GPU, disks, network', monitorSheet));
  const power = h('div', { class: 'card' },
    row('restart', 'Restart', 'Hold for a timer', () => act('restart', {}, 'Restarting…'),
        { strong: true, onLong: () => timers('restart', 'Restart', { force: true }) }),
    row('power', 'Shut down', 'Hold for a timer', () => act('shutdown', {}, 'Shutting down…'),
        { danger: true, strong: true, onLong: () => timers('shutdown', 'Shut down', { force: true }) }),
    row('snow', 'Hibernate', null, () => act('hibernate', {}, 'Hibernating…'), { strong: true }),
    row('logout', 'Sign out', 'Open apps will close', () => act('signout', {}, 'Signing out…'), { strong: true }));
  const extra = Object.entries(cmds).filter(([n, m]) => !m.hide && !KNOWN.has(n));
  root.replaceChildren(h('div', {},
    h('div', { class: 'ctl-head' }, h('div', {}, h('h1', {}, INFO.host || 'PC'),
      h('button', { class: 'sub', id: 'ctl-stats', title: 'System monitor', onclick: () => { haptic(5); monitorSheet(); } })), tabSwitch()),
    h('div', { id: 'ctl-banner' }),
    tiles,
    h('div', { class: 'card pad-x' },
      liveSlider('volume', muted ? 'mute' : 'volume', 'Volume', vol),
      bright == null ? h('div', { class: 'slider', style: 'opacity:.5', title: s.brightness?.error || '' },
        h('div', { class: 'lbl' }, ico('sun'), h('span', {}, 'Brightness unavailable'))) : liveSlider('brightness', 'sun', 'Brightness', bright)),
    h('div', { class: 'group-title' }, 'Connections'), radios,
    h('div', { class: 'group-title' }, 'Tools'), tools,
    h('div', { class: 'group-title' }, 'Power'), power,
    extra.length ? h('div', { class: 'group-title' }, 'More') : null,
    extra.length ? h('div', { class: 'card' }, extra.map(([n, m]) => row(m.icon || 'bolt', m.title, m.description,
      () => genericCommand(n, m), { strong: m.confirm && !m.params.length, danger: m.danger }))) : null,
    h('div', { class: 'group-title' }, 'About'),
    h('div', { class: 'card' },
      row('info', 'About this PC', INFO.host ? `${INFO.host} · system, network, server` : 'System, network, server', aboutSheet),
      INFO.https ? row('shield', 'Secure connection', secureNow() ? 'On (HTTPS)' : 'Install the certificate for the HD stream', certSheet) : null,
      row('download', 'Install as app', 'Full screen, from your home screen', installSheet))));
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

const secureNow = () => location.protocol === 'https:' && window.isSecureContext;

async function pollStats() {
  if (!active) return;
  try { stats = await run('stats'); } catch {}
  try {  // round trip: a tiny request on the already-open connection
    const t = performance.now();
    await fetch(url('/ping'), { cache: 'no-store' });
    rtt = performance.now() - t;
  } catch { rtt = null; }
  renderStats();
  statsT = setTimeout(pollStats, 2000);
}
function renderStats() {
  const el = document.getElementById('ctl-stats');
  if (!el) return;
  const s = stats, parts = [];
  if (s.cpu != null) parts.push(h('span', {}, 'CPU ', h('b', {}, Math.round(s.cpu) + '%')));
  if (s.ram != null) parts.push(h('span', {}, 'RAM ', h('b', {}, s.ram + '%')));
  if (s.gpu != null) parts.push(h('span', {}, 'GPU ', h('b', {}, s.gpu + '%'), s.gpu_temp ? ` ${s.gpu_temp}°` : ''));
  if (rtt != null) parts.push(h('span', { title: 'Round trip to the PC' }, 'RTT ', h('b', {}, rtt.toFixed(1) + ' ms')));
  if (s.uptime_s) parts.push(h('span', {}, 'Up ', h('b', {}, fmtDuration(s.uptime_s))));
  el.replaceChildren(...parts);
}

// ------------------------------------------------------ system monitor ---
const fmtRate = b => b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB/s' : b >= 1e3 ? (b / 1e3).toFixed(0) + ' KB/s' : Math.round(b) + ' B/s';
const gb = b => (b / 2 ** 30).toFixed(b >= 100 * 2 ** 30 ? 0 : 1);
// two rates with one shared unit: '76.1 / 18.3 MB/s'
const pair = (a, b) => { const m = Math.max(a, b), [d, u] = m >= 1e6 ? [1e6, 'MB/s'] : m >= 1e3 ? [1e3, 'KB/s'] : [1, 'B/s'];
  const f = v => (v / d).toFixed(d > 1 && m / d < 100 ? 1 : 0); return `${f(a)} / ${f(b)} ${u}`; };
// load 0..100 -> a colour from calm blue to hot orange
const heat = v => `color-mix(in srgb, var(--mon-hot) ${Math.round(Math.min(100, Math.max(0, v)) ** 1.25 / 100 ** 0.25)}%, var(--mon-cool))`;
// The graphs show the last minute; at 20 samples a second that's up to
// 1200 points, drawn from at most SPARK_PTS averaged (or, for rates, peak) buckets.
const SPARK_PTS = 240;
function downsample(vals, peak) {
  if (vals.length <= SPARK_PTS) return vals;
  const out = [], step = vals.length / SPARK_PTS;
  for (let i = 0; i < SPARK_PTS; i++) {
    const part = vals.slice(Math.floor(i * step), Math.floor((i + 1) * step));
    out.push(peak ? Math.max(...part) : part.reduce((a, b) => a + b, 0) / part.length);
  }
  return out;
}
function spark(series, { max = 100, colors = ['var(--mon-cool)'], window = 1200 } = {}) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 100 20');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.classList.add('spark');
  const draw = data => {
    const pts = data.map(v => downsample(v.slice(-window), !max));
    const top = max || Math.max(1, ...pts.flat());
    const span = Math.min(window, SPARK_PTS) - 1;
    svg.innerHTML = pts.map((vals, k) => {
      if (vals.length < 2) return '';
      const n = vals.length, c = colors[k % colors.length];
      const xy = vals.map((v, i) => `${(100 - (n - 1 - i) * 100 / span).toFixed(2)},${(19.5 - 19 * Math.min(1, v / top)).toFixed(2)}`);
      return `<path d="M${xy[0].split(',')[0]},20 L${xy.join(' L')} L100,20Z" fill="${c}" opacity=".16"/>` +
             `<path d="M${xy.join(' L')}" fill="none" stroke="${c}" stroke-width="1.3" vector-effect="non-scaling-stroke"/>`;
    }).join('');
  };
  draw(series);
  return { el: svg, draw };
}
const bar = (parts, cls = '') => {  // parts: [[fraction, colour], ...]
  const el = h('div', { class: 'mon-bar ' + cls });
  const set = ps => el.replaceChildren(...ps.map(([f, c]) => h('i', { style: `width:${Math.max(0, Math.min(1, f)) * 100}%;background:${c}` })));
  set(parts);
  return { el, set };
};

// Live: the server pushes a sample 20 times a second over a WebSocket; the
// page draws at most once per screen frame.
function monitorSheet() {
  const body = h('div', { class: 'mon' }, h('div', { class: 'empty' }, 'Connecting…'));
  const st = { static: null, hist: null, slow: {}, cur: null, every: 0.05 };
  let ws = null, open = true, update = null, dirty = false, pingT = 0, retryT = 0;
  const frame = () => {
    if (!open || !dirty) return;
    dirty = false;
    if (!update) update = buildMonitor(body, st);
    update(st);
  };
  const connect = () => {
    if (!open || document.hidden) return;
    ws = new WebSocket(wsUrl('/sysmon/live'));
    ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.t === 'hello') {
        Object.assign(st, { static: m.cpu, hist: m.history, slow: m.slow || {}, every: m.every_s });
        return;
      }
      if (m.slow) st.slow = m.slow;
      st.cur = m;
      const cap = Math.round(60 / st.every);
      const push = (k, v) => { const a = st.hist[k]; a.push(v); if (a.length > cap) a.splice(0, a.length - cap); };
      push('cpu', m.cpu.total ?? 0);
      push('mem', m.mem.total ? 100 * m.mem.used / m.mem.total : 0);
      push('gpu', m.gpu?.util ?? 0);
      push('net_rx', m.net.rx); push('net_tx', m.net.tx);
      push('disk_r', m.disk.read); push('disk_w', m.disk.write);
      if (!dirty) { dirty = true; requestAnimationFrame(frame); }
    };
    ws.onclose = () => { ws = null; clearTimeout(retryT); if (open) retryT = setTimeout(connect, 1000); };
  };
  const onVis = () => { if (document.hidden) ws?.close(); else if (!ws) connect(); };
  document.addEventListener('visibilitychange', onVis);
  pingT = setInterval(() => ws?.readyState === 1 && ws.send('{"t":"ping"}'), 15000);
  sheet({ title: 'System', body, wide: true, onClose: () => {
    open = false; clearInterval(pingT); clearTimeout(retryT);
    document.removeEventListener('visibilitychange', onVis);
    ws?.close();
  } });
  connect();
}

function buildMonitor(body, st) {
  const card = (title, ...kids) => h('div', { class: 'mon-card' }, h('div', { class: 'mon-k' }, title), ...kids);
  const txt = (cls = '') => h('span', { class: cls });
  const up = [];  // updaters, called with the state on each drawn frame
  // Numbers change 20 times a second; a light smoothing keeps them readable
  // (bars and graphs show the raw samples).
  const smooth = {};
  const sm = (k, v, a = 0.3) => (smooth[k] = smooth[k] == null || v == null ? v : smooth[k] + a * (v - smooth[k]));

  // ---- CPU: a map of the cores; performance cores are bigger tiles with a bar per thread
  const cores = st.static.cores, units = cores.reduce((a, c) => a + c.threads.length, 0);
  const nP = cores.filter(c => c.kind === 'P').length, nE = cores.filter(c => c.kind === 'E').length;
  const cpuBig = txt('mon-big'), cpuClock = txt(), cpuSpark = spark([[]]);
  const map = h('div', { class: 'cores', style: `grid-template-columns:repeat(${Math.min(units, 24)},1fr)` });
  let pi = 0, ei = 0, ci = 0;
  const tiles = cores.map(c => {
    const label = c.kind === 'P' ? 'P' + ++pi : c.kind === 'E' ? 'E' + ++ei : String(++ci);
    const fills = c.threads.map(() => h('b')), ghz = txt('ghz');
    map.append(h('div', { class: `core ${c.kind}`, style: `grid-column:span ${Math.min(24, c.threads.length)}` },
      h('div', { class: 'bars' }, fills.map(f => h('i', {}, f))), h('span', { class: 'lbl' }, label), ghz));
    return { c, fills, ghz };
  });
  up.push(({ cur, hist }) => {
    const c = cur.cpu;
    cpuBig.replaceChildren(String(Math.round(sm('cpu', c.total ?? 0))), h('small', {}, '%'));
    const clk = sm('clock', c.clock);
    cpuClock.textContent = clk ? `${(clk / 1000).toFixed(2)} GHz` : '';
    cpuSpark.draw([hist.cpu]);
    for (const t of tiles) {
      t.c.threads.forEach((i, k) => {
        const v = c.threads[i] ?? 0;
        t.fills[k].style.height = Math.max(2, v) + '%';
        t.fills[k].style.background = heat(v);
      });
      const m = sm('mhz' + t.c.threads[0], Math.max(0, ...t.c.threads.map(i => c.mhz[i] || 0)), 0.2);
      t.ghz.textContent = m ? (m / 1000).toFixed(1) : '';
    }
  });
  const cpuCard = card('Processor',
    h('div', { class: 'mon-head' }, cpuBig, h('div', { class: 'mon-side' },
      h('div', {}, st.static.name || 'CPU'),
      h('div', {}, cpuClock, ` · ${nP ? `${nP}P + ${nE}E cores` : `${cores.length} cores`} · ${units} threads`))),
    cpuSpark.el, map,
    nP ? h('div', { class: 'mon-note' }, 'Taller tiles are performance cores (one bar per thread), smaller ones efficiency cores. Numbers are GHz.') : null);

  // ---- memory
  const memBig = txt('mon-big'), memSide = txt(), memBar = bar([]), memLegend = h('div', { class: 'mon-legend' });
  const memRows = h('div', { class: 'mon-rows' }), memSpark = spark([[]]);
  up.push(({ cur, hist }) => {
    const m = cur.mem, free = m.total - m.used;
    memBig.replaceChildren(String(Math.round(100 * m.used / m.total)), h('small', {}, '%'));
    memSide.textContent = `${gb(m.used)} of ${gb(m.total)} GB in use`;
    memBar.set([[m.used / m.total, 'var(--mon-cool)'], [m.cached / m.total, 'color-mix(in srgb, var(--mon-cool) 35%, transparent)']]);
    memLegend.replaceChildren(
      h('span', {}, h('i', { style: 'background:var(--mon-cool)' }), `In use ${gb(m.used)} GB`),
      h('span', {}, h('i', { style: 'background:color-mix(in srgb, var(--mon-cool) 35%, transparent)' }), `Cached ${gb(m.cached)} GB`),
      h('span', {}, h('i', { style: 'background:var(--fill)' }), `Free ${gb(Math.max(0, free - m.cached))} GB`));
    memRows.replaceChildren(kv('Committed', `${gb(m.commit)} / ${gb(m.commit_limit)} GB`),
      kv('Kernel pools', `${gb(m.paged)} paged · ${gb(m.nonpaged)} non-paged GB`));
    memSpark.draw([hist.mem]);
  });
  const memCard = card('Memory', h('div', { class: 'mon-head' }, memBig, h('div', { class: 'mon-side' }, h('div', {}, memSide))),
    memSpark.el, memBar.el, memLegend, memRows);

  // ---- GPU
  let gpuCard = null;
  if (st.cur.gpu) {
    const gBig = txt('mon-big'), gSpark = spark([[]]), vram = bar([]), vramTxt = txt(), stats = h('div', { class: 'mon-stats' });
    const stat = (label, value, frac) => h('div', { class: 'mon-stat' }, h('div', { class: 'k' }, label), h('div', { class: 'v' }, value),
      frac == null ? null : bar([[frac, heat(frac * 100)]], 'thin').el);
    up.push(({ cur, hist, slow }) => {
      const g = cur.gpu, pw = sm('gpu_w', g.power_w);
      gBig.replaceChildren(String(Math.round(sm('gpu', g.util ?? 0))), h('small', {}, '%'));
      gSpark.draw([hist.gpu]);
      if (g.vram_total) {
        vram.set([[g.vram_used / g.vram_total, 'var(--mon-cool)']]);
        vramTxt.textContent = `VRAM ${gb(g.vram_used)} of ${gb(g.vram_total)} GB`;
      } else vramTxt.textContent = g.vram_used ? `VRAM ${gb(g.vram_used)} GB in use` : '';
      stats.replaceChildren(...[
        g.temp != null && stat('Temperature', `${g.temp} °C`, g.temp / 90),
        pw != null && stat('Power', `${Math.round(pw)}${g.power_limit_w ? ` / ${Math.round(g.power_limit_w)}` : ''} W`, g.power_limit_w ? pw / g.power_limit_w : null),
        g.clock != null && stat('Core clock', `${g.clock} MHz`, g.clock_max ? g.clock / g.clock_max : null),
        g.mem_clock != null && stat('Memory clock', `${g.mem_clock} MHz`),
        g.fan != null && stat('Fan', `${g.fan}%`, g.fan / 100),
        g.encoder != null && stat('Video encode / decode', `${g.encoder}% / ${g.decoder ?? 0}%`, Math.max(g.encoder, g.decoder || 0) / 100),
        slow.pcie && stat('PCIe ↓ in / ↑ out', pair(slow.pcie.rx * 1024, slow.pcie.tx * 1024)),
      ].filter(Boolean));
    });
    gpuCard = card('Graphics', h('div', { class: 'mon-head' }, gBig, h('div', { class: 'mon-side' }, h('div', {}, st.cur.gpu.name || 'GPU'), h('div', {}, vramTxt))),
      gSpark.el, vram.el, stats);
  }

  // ---- disks and network
  const two = { max: 0, colors: ['var(--mon-cool)', 'var(--mon-hot)'] };
  const dRates = h('div', { class: 'mon-rates' }), dSpark = spark([[], []], two), vols = h('div', { class: 'mon-vols' });
  let volsShown = null;
  up.push(({ cur, hist, slow }) => {
    const k = cur.disk;
    dRates.replaceChildren(h('span', { class: 'rd' }, `Read ${fmtRate(sm('dr', k.read, 0.15))}`), h('span', { class: 'wr' }, `Write ${fmtRate(sm('dw', k.write, 0.15))}`),
      k.active != null ? h('span', {}, `${Math.round(sm('da', k.active, 0.15))}% active`) : null);
    dSpark.draw([hist.disk_r, hist.disk_w]);
    if (slow.volumes && slow.volumes !== volsShown) {
      volsShown = slow.volumes;
      vols.replaceChildren(...slow.volumes.map(v => h('div', { class: 'mon-vol' },
        h('div', { class: 'row1' }, h('b', {}, v.name), h('span', {}, v.label), h('span', { class: 'v' }, `${gb(v.total - v.used)} GB free of ${gb(v.total)}`)),
        bar([[v.used / v.total, heat(100 * v.used / v.total)]], 'thin').el)));
    }
  });
  const nRates = h('div', { class: 'mon-rates' }), nSpark = spark([[], []], two);
  up.push(({ cur, hist }) => {
    nRates.replaceChildren(h('span', { class: 'rd' }, `↓ ${fmtRate(sm('rx', cur.net.rx, 0.15))}`), h('span', { class: 'wr' }, `↑ ${fmtRate(sm('tx', cur.net.tx, 0.15))}`));
    nSpark.draw([hist.net_rx, hist.net_tx]);
  });
  const diskCard = card('Disks', dRates, dSpark.el, vols);
  const netCard = card('Network', nRates, nSpark.el);

  // ---- busiest apps and system counts (the server refreshes these once a second)
  const procs = h('div', { class: 'mon-procs' }), foot = h('div', { class: 'mon-foot' });
  let slowShown = null;
  up.push(({ slow }) => {
    if (slow === slowShown || !slow.procs) return;
    slowShown = slow;
    const top = Math.max(5, ...slow.procs.map(p => p.cpu));
    procs.replaceChildren(...slow.procs.map(p => h('div', { class: 'mon-proc' },
      h('span', { class: 'nm' }, p.name), h('span', { class: 'cpu' }, `${p.cpu.toFixed(1)}%`),
      h('span', { class: 'mem' }, p.mem >= 2 ** 30 ? `${gb(p.mem)} GB` : `${Math.round(p.mem / 2 ** 20)} MB`),
      bar([[p.cpu / top, heat(p.cpu * 4)]], 'thin').el)));
    const c = slow.counts || {};
    foot.textContent = [c.processes && `${c.processes} processes`, c.threads && `${c.threads.toLocaleString()} threads`,
      c.handles && `${c.handles.toLocaleString()} handles`, slow.uptime_s && `up ${fmtDuration(slow.uptime_s)}`,
      slow.battery ? `battery ${slow.battery.percent}%${slow.battery.charging ? ' ⚡' : ''}` : null, 'updates 20× a second'].filter(Boolean).join(' · ');
  });
  const procCard = card('Busiest apps', procs);

  body.replaceChildren(h('div', { class: 'mon-grid' }, cpuCard, memCard, gpuCard, diskCard, netCard, procCard), foot);
  return s => up.forEach(f => f(s));
}

// --------------------------------------------------------------- about ---
const kv = (k, v, extra) => v == null || v === '' ? null
  : h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v' }, v), extra || null);
const copyBtn = (text, label = 'Copy') => h('button', { class: 'kv-copy', title: text, onclick: async e => {
  e.stopPropagation(); haptic(5); toast((await copyToDevice(text)) ? 'Copied ' + text : 'Copy failed', { ic: 'check', ms: 1400 });
} }, label);
const netName = ip => /^192\.168\.|^10\./.test(ip) ? 'Local network'
  : /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(ip) ? 'Tailscale'
  : 'Other';  // VPNs, WSL / Hyper-V adapters, …
const when = t => new Date(t * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });

async function aboutSheet() {
  const s = sheet({ title: 'About this PC', body: h('div', { class: 'empty' }, 'Loading…') });
  try { about = await run('about'); } catch (e) { s.setBody(h('div', { class: 'empty' }, e.message)); return; }
  const a = about, port = location.port || (location.protocol === 'https:' ? 443 : 80);
  const plan = resolvePlan(), method = METHODS.find(m => m.id === plan.mode)?.name;
  const ips = [...a.ips].sort((x, y) => (netName(x) === 'Local network' ? 0 : netName(x) === 'Tailscale' ? 1 : 2)
                                     - (netName(y) === 'Local network' ? 0 : netName(y) === 'Tailscale' ? 1 : 2));
  s.setBody(h('div', {},
    h('div', { class: 'group-title' }, 'PC'),
    h('div', { class: 'card' },
      kv('Name', a.host), kv('Windows', a.windows), kv('Processor', a.cpu && `${a.cpu.replace(/\((R|TM)\)/g, '').replace(/\s+/g, ' ')} · ${a.cores} threads`),
      kv('Graphics', a.gpus.join(', ')), kv('Memory', a.ram_gb && `${Math.round(a.ram_gb)} GB`),
      ...a.displays.map(d => kv(d.name + (d.primary ? ' (main)' : ''), `${d.w} × ${d.h} · ${d.hz} Hz`)),
      kv('Up for', a.uptime_s && fmtDuration(a.uptime_s))),
    h('div', { class: 'group-title' }, 'This device'),
    h('div', { class: 'card' },
      kv('Connection', secureNow() ? 'Secure (HTTPS)' : 'Plain HTTP'),
      kv('Stream', method && (plan.mode === 'jpeg' && a.tiles ? 'JPEG, changed areas only' : method) + (plan.alt ? ', H.264 on slow links' : '')),
      kv('Round trip', rtt != null ? rtt.toFixed(1) + ' ms' : null)),
    h('div', { class: 'group-title' }, 'Addresses'),
    h('div', { class: 'card addr' }, ips.map(ip => kv(netName(ip), `${ip}:${port}`,
      h('span', { class: 'kv-btns' }, copyBtn(`http://${ip}:${port}/`, 'http'),
        INFO.https ? copyBtn(`https://${ip}:${port}/`, 'https') : null)))),
    h('div', { class: 'group-title' }, 'Server'),
    h('div', { class: 'card' },
      kv('Version', a.version && (a.version + (a.version_time ? ` · ${when(a.version_time)}` : ''))),
      kv('Running for', fmtDuration(a.server_up_s)),
      kv('Access token', a.token ? 'Set' : 'Not set — anyone on your network can use this'),
      kv('Video encoder', [...(a.encoders.h264 || []), ...(a.encoders.hevc || [])].join(', ') || 'None found'),
      kv('JPEG', a.tiles ? 'Changed areas only' : 'Whole frames (install numpy, simplejpeg)'),
      kv('ffmpeg', a.ffmpeg || 'Not found'), kv('Python', a.python)),
    a.token ? null : h('div', { class: 'foot', style: 'text-align:left;margin:10px 4px 0' },
      'To require a password, set PC_API_TOKEN in launch_remote.bat and open this page once with ?token=… .')));
}

// The certificate install guide, for the platform this page runs on.
const PLATFORMS = [
  ['ios', 'iPhone / iPad', [
    'Tap Download certificate below (in Safari), then Allow.',
    'Open Settings → General → VPN & Device Management, tap the PC Remote profile and Install.',
    'Open Settings → General → About → Certificate Trust Settings and turn on PC Remote CA.',
    'Tap Open secure address.']],
  ['android', 'Android', [
    'Tap Download certificate below.',
    'Open Settings and search for “CA certificate” (usually Security → Encryption & credentials → Install a certificate → CA certificate).',
    'Choose Install anyway, then pick ca.crt from Downloads.',
    'Tap Open secure address. (Chrome trusts it; some other apps don’t.)']],
  ['windows', 'Windows', [
    'Download the certificate and open ca.crt.',
    'Choose Install Certificate → Current User → Place all certificates in the following store → Browse → Trusted Root Certification Authorities.',
    'Finish, confirm with Yes, then restart the browser. (Firefox has its own list: Settings → Privacy & Security → Certificates → View Certificates → Import.)']],
  ['mac', 'Mac', [
    'Download the certificate and open ca.crt; Keychain Access adds it to the login keychain.',
    'In Keychain Access, double-click PC Remote CA → Trust → When using this certificate: Always Trust.',
    'Close the window (enter your password), then reload the secure address.']],
];
function platform() {
  const ua = navigator.userAgent;
  if (/iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return 'ios';
  if (/Android/.test(ua)) return 'android';
  if (/Windows/.test(ua)) return 'windows';
  if (/Macintosh/.test(ua)) return 'mac';
  return 'windows';
}
async function certSheet() {
  let pick = platform();
  const steps = h('div'), seg = h('div', { class: 'seg', style: 'margin:0 0 10px' });
  const draw = () => {
    seg.replaceChildren(...PLATFORMS.map(([id, name]) => h('button', { class: id === pick ? 'on' : '', onclick: () => { pick = id; haptic(5); draw(); } }, name)));
    steps.replaceChildren(h('ol', { class: 'steps' }, PLATFORMS.find(p => p[0] === pick)[2].map(t => h('li', {}, t))));
  };
  draw();
  const fp = h('div');
  const s = sheet({ title: 'Secure connection', body: h('div', { class: 'form' },
    h('div', { class: 'note' + (secureNow() ? ' ok' : '') }, secureNow()
      ? 'This page is on the secure address, so the HD stream (H.264, lowest latency and data) is available. Install the certificate on other devices the same way.'
      : 'Browsers only allow the HD stream (H.264: lowest latency and data) on secure pages. The PC makes its own certificate; install it once on this device and the secure address opens without warnings.'),
    h('div', { style: 'overflow-x:auto' }, seg), steps,
    h('div', { class: 'btns' },
      h('a', { class: 'btn gray', href: '/ca.crt', style: 'display:grid;place-items:center;text-decoration:none' }, 'Download certificate'),
      secureNow() ? h('button', { class: 'btn', onclick: () => s.close() }, 'Done')
        : h('a', { class: 'btn', href: INFO.https, style: 'display:grid;place-items:center;text-decoration:none' }, 'Open secure address')),
    fp) });
  try {
    const c = (about ||= await run('about')).certificate;
    if (c) fp.replaceChildren(h('div', { class: 'foot', style: 'text-align:left;margin:4px 2px 0' },
      'To check it’s the right one, its SHA-256 fingerprint is ', h('span', { class: 'fp' }, c.sha256), `. Valid until ${when(c.expires)}.`));
  } catch {}
}

function installSheet() {
  const p = platform(), standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  const how = { ios: 'In Safari, tap Share (the square with an arrow), then Add to Home Screen.',
    android: 'In Chrome, tap ⋮ → Add to Home screen (or Install app).',
    windows: 'In Chrome or Edge, click the install icon at the right end of the address bar (or ⋮ → Install).',
    mac: 'In Safari, choose File → Add to Dock; in Chrome, ⋮ → Save and share → Install page as app.' }[p];
  const s = sheet({ title: 'Install as app', body: h('div', { class: 'form' },
    h('div', { class: 'note' + (standalone ? ' ok' : '') }, standalone
      ? 'You’re already using the installed app.'
      : 'Opened from your home screen, the remote runs full screen without the browser’s bars, like a native app.'),
    standalone ? null : h('div', { style: 'font-size:15px;line-height:1.45' }, how),
    installPrompt && !standalone ? h('button', { class: 'btn', onclick: async () => {
      installPrompt.prompt(); await installPrompt.userChoice.catch(() => {}); installPrompt = null; s.close();
    } }, 'Install') : null) });
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
