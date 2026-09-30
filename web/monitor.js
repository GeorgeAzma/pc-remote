// System tab: a live monitor of the PC. The server pushes a sample 20 times
// a second over a WebSocket while this tab is showing (and only then); the
// page draws at most once per screen frame. While it's open the phone's
// screen is kept awake (where the browser allows it: secure pages), so a
// propped-up phone works as a second screen for the PC.
import { h, wsUrl, fmtDuration } from './app.js';

const $ = id => document.getElementById(id);
const kv = (k, v) => v == null || v === '' ? null
  : h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v' }, v));

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

// ---------------------------------------------------------- lifecycle ---
const st = { static: null, hist: null, slow: {}, cur: null, every: 0.05 };
let active = false, ws = null, update = null, dirty = false, retryT = 0, wake = null;

function frame() {
  if (!active || !dirty) return;
  dirty = false;
  if (!update) update = buildMonitor($('mon-body'), st);
  update(st);
}
function connect() {
  if (ws || !active || document.hidden) return;
  const sock = ws = new WebSocket(wsUrl('/sysmon/live'));
  sock.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.t === 'hello') {
      Object.assign(st, { static: m.cpu, hist: m.history, slow: m.slow || {}, every: m.every_s });
      $('mon-sub').textContent = `Live · ${Math.round(1 / m.every_s)} updates a second`;
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
  sock.onclose = () => {
    if (ws === sock) ws = null;
    clearTimeout(retryT);
    if (active && !document.hidden) retryT = setTimeout(connect, 1000);
  };
}
async function keepAwake(on) {
  try {
    if (on && !wake && 'wakeLock' in navigator) {
      wake = await navigator.wakeLock.request('screen');
      wake.addEventListener('release', () => { wake = null; });
    } else if (!on && wake) {
      await wake.release();
      wake = null;
    }
  } catch {}  // not allowed here (plain HTTP) or refused: the screen just sleeps as usual
}
setInterval(() => ws?.readyState === 1 && ws.send('{"t":"ping"}'), 15000);
document.addEventListener('visibilitychange', () => {
  if (!active) return;
  if (document.hidden) ws?.close();
  else { connect(); keepAwake(true); }  // the browser drops the wake lock while hidden
});

export function show() {
  active = true;
  connect();
  keepAwake(true);
}
export function hide() {
  active = false;
  ws?.close();
  keepAwake(false);
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
