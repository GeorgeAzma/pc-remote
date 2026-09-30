// Terminal tab: renders the VT emulator's dirty rows to the DOM and wires
// keyboard / touch input to a persistent ConPTY session.
import { settings, setSetting, h, toast, haptic, wsUrl, INFO, choose, copyToDevice } from './app.js';
import { hydrateIcons } from './icons.js';
import { Emulator, PAL, F, FG0, BG0, attrs } from './vt.js';

const $ = id => document.getElementById(id);
const screenEl = $('term-screen'), rowsEl = $('term-rows'), ta = $('term-input');

// ------------------------------------------------------------ renderer ---
const esc = s => s.replace(/[&<>]/g, c => c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;');
const color = v => v === null ? null : typeof v === 'number' ? PAL[v] : v;
function rowHtml(line, cx) {
  let out = '', run = '', cur = -1;
  const flush = () => {
    if (!run) return;
    const a = attrs[cur];
    let fg = color(a.fg), bg = color(a.bg);
    if (a.fl & F.inverse) { [fg, bg] = [bg || BG0, fg || FG0]; }
    const cls = [(a.fl & F.bold) && 'b', (a.fl & F.dim) && 'dim', (a.fl & F.italic) && 'it', (a.fl & F.under) && 'u',
                 (a.fl & F.strike) && 's', (a.fl & F.hidden) && 'inv-h'].filter(Boolean).join(' ');
    const st = (fg ? `color:${fg};` : '') + (bg ? `background:${bg};` : '');
    out += cls || st ? `<span${cls ? ` class="${cls}"` : ''}${st ? ` style="${st}"` : ''}>${esc(run)}</span>` : esc(run);
    run = '';
  };
  const n = line.ch.length;
  for (let x = 0; x < n; x++) {
    const ch = line.ch[x];
    if (ch === '') continue;  // second half of a wide char
    if (x === cx) {
      flush();
      out += `<span class="c blink">${esc(ch === ' ' ? ' ' : ch)}</span>`;
      cur = -1;
      continue;
    }
    if (line.at[x] !== cur) { flush(); cur = line.at[x]; }
    run += ch;
  }
  flush();
  return out.replace(/ +$/, '') || ' ';
}

// ----------------------------------------------------------- terminal ---
let emu = null, ws = null, sid = {}, shell = settings.shell || 'ps', exited = false, active = false;
let cell = { w: 7.8, h: 16 }, raf = 0, stick = true, screenDivs = [], sbCount = 0, retry = 0;
try { sid = JSON.parse(localStorage.getItem('pc.termsid') || '{}'); } catch {}
const MAX_SB_DOM = 1500;

function measure() {
  rowsEl.style.fontSize = settings.termFont + 'px';
  const probe = h('span', { style: 'visibility:hidden;position:absolute;white-space:pre' }, 'W'.repeat(80));
  rowsEl.append(probe);
  const w = probe.getBoundingClientRect().width / 80;
  probe.remove();
  if (w > 0) cell = { w, h: settings.termFont * 1.25 };
}
// Null while the terminal isn't laid out (hidden tab, page restored in the
// background): sizing from that would tell ConPTY to be 20x5.
function gridSize() {
  const cs = getComputedStyle(rowsEl);
  const w = rowsEl.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - 2;
  const hh = rowsEl.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
  if (w < cell.w * 10 || hh < cell.h * 3) return null;
  return [Math.max(20, Math.floor(w / cell.w)), Math.max(5, Math.floor(hh / cell.h))];
}
function buildDom() {
  rowsEl.replaceChildren();
  screenDivs = [];
  sbCount = 0;
  for (const l of emu.scrollback.slice(-MAX_SB_DOM)) addScrollDiv(l);
  for (let y = 0; y < emu.rows; y++) {
    const d = h('div', { class: 'term-row' });
    rowsEl.append(d);
    screenDivs.push(d);
  }
  emu.all = true;
  schedule();
}
function addScrollDiv(l) {
  const d = h('div', { class: 'term-row', html: rowHtml(l, -1) });
  rowsEl.insertBefore(d, screenDivs[0] || null);
  if (++sbCount > MAX_SB_DOM) { rowsEl.firstChild.remove(); sbCount--; }
}
function schedule() { if (!raf) raf = requestAnimationFrame(render); }
function render() {
  raf = 0;
  if (!emu) return;
  const rows = emu.all ? [...Array(emu.rows).keys()] : [...emu.dirty];
  emu.all = false;
  emu.dirty.clear();
  const showCursor = emu.modes.cursor && !exited && rowsEl.scrollTop + rowsEl.clientHeight >= rowsEl.scrollHeight - cell.h * 2;
  for (const y of rows) {
    const d = screenDivs[y];
    if (d && emu.buf[y]) d.innerHTML = rowHtml(emu.buf[y], showCursor && y === emu.y ? emu.x : -1);
  }
  if (stick) rowsEl.scrollTop = rowsEl.scrollHeight;
}
rowsEl.addEventListener('scroll', () => { stick = rowsEl.scrollTop + rowsEl.clientHeight >= rowsEl.scrollHeight - 4; });

function makeEmu() {
  measure();
  const g = gridSize();
  if (!g) return false;
  emu = new Emulator(g[0], g[1], s => sendData(s));
  emu.onScroll = l => {
    // the old top screen row becomes the newest scrollback row
    const d = screenDivs.shift();
    d.innerHTML = rowHtml(l, -1);
    sbCount++;
    if (sbCount > MAX_SB_DOM) { rowsEl.firstChild.remove(); sbCount--; }
    const nd = h('div', { class: 'term-row' });
    rowsEl.append(nd);
    screenDivs.push(nd);
  };
  emu.onClearScroll = () => { while (sbCount > 0) { rowsEl.firstChild.remove(); sbCount--; } };
  buildDom();
  return true;
}

const decoder = { d: new TextDecoder() };
function connect(fresh = false) {
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  if (!emu && !makeEmu()) return;  // not laid out yet: the ResizeObserver retries
  setDot('warn');
  const [cols, rows] = [emu.cols, emu.rows];
  const s = new WebSocket(wsUrl('/term', { sid: sid[shell] || '', shell, cols, rows }));
  ws = s;
  s.binaryType = 'arraybuffer';
  s.onopen = () => {
    retry = 0; setDot('ok');
    if (fresh) s.send(JSON.stringify({ t: 'restart', shell }));
    // A resize computed while connecting was dropped; ConPTY must know the
    // real grid or its repaints scroll our screen out of sync.
    s.send(JSON.stringify({ t: 'resize', cols: emu.cols, rows: emu.rows }));
  };
  s.onmessage = e => {
    if (typeof e.data !== 'string') {
      emu.write(decoder.d.decode(new Uint8Array(e.data), { stream: true }));
      schedule();
      return;
    }
    const m = JSON.parse(e.data);
    if (m.t === 'ready') {
      sid[shell] = m.sid;
      localStorage.setItem('pc.termsid', JSON.stringify(sid));
      exited = false;
      decoder.d = new TextDecoder();
      emu.reset();          // the server replays the session's output
      buildDom();
    } else if (m.t === 'exit') {
      exited = true;
      emu.write(`\r\n\x1b[2m[process exited${m.code >= 0 ? ' with code ' + m.code : ''} — press Enter to restart]\x1b[0m\r\n`);
      schedule();
      setDot('');
    } else if (m.t === 'error') {
      toast(m.msg, { err: true });
    }
  };
  s.onclose = () => {
    if (ws !== s) return;
    ws = null;
    setDot('bad');
    if (active && !document.hidden) setTimeout(() => active && !ws && connect(), Math.min(3000, 300 * 2 ** retry++));
  };
}
setInterval(() => ws?.readyState === 1 && ws.send(JSON.stringify({ t: 'hb' })), 15000);
function setDot(k) { $('term-dot').className = 'dot ' + k; }

function sendData(s) {
  if (!ws || ws.readyState !== 1) return;
  ws.send(new TextEncoder().encode(s));
}
function sendInput(s) {
  if (exited) {
    if (s === '\r') { exited = false; connect(true); }
    return;
  }
  stick = true;
  sendData(s);
}

let resizeT = 0;
function onResize() {
  if (!active) return;
  clearTimeout(resizeT);
  resizeT = setTimeout(() => {
    if (!emu) { if (!ws) connect(); return; }
    const g = gridSize();
    if (!g) return;
    const [c, r] = g;
    if (c === emu.cols && r === emu.rows) return;
    emu.resize(c, r);
    buildDom();
    ws?.readyState === 1 && ws.send(JSON.stringify({ t: 'resize', cols: c, rows: r }));
  }, 80);
}
new ResizeObserver(onResize).observe(screenEl);

// -------------------------------------------------------------- input ---
const mods = { ctrl: false, alt: false };
function applyMods(s) {
  if (mods.ctrl && s.length === 1) {
    const c = s.toUpperCase().charCodeAt(0);
    if (c >= 64 && c <= 95) s = String.fromCharCode(c - 64);
    else if (s === ' ') s = '\x00';
    else if (s === '/') s = '\x1f';
  }
  if (mods.alt) s = '\x1b' + s;
  if (mods.ctrl || mods.alt) { mods.ctrl = mods.alt = false; renderAcc(); }
  return s;
}
function keySeq(e) {
  const app = emu?.modes.ckm;
  const arrows = { ArrowUp: 'A', ArrowDown: 'B', ArrowRight: 'C', ArrowLeft: 'D' };
  if (arrows[e.key]) {
    const m = (e.shiftKey ? 1 : 0) + (e.altKey ? 2 : 0) + (e.ctrlKey ? 4 : 0);
    return m ? `\x1b[1;${m + 1}${arrows[e.key]}` : (app ? '\x1bO' : '\x1b[') + arrows[e.key];
  }
  const map = { Enter: '\r', Backspace: e.ctrlKey ? '\x08' : '\x7f', Tab: e.shiftKey ? '\x1b[Z' : '\t', Escape: '\x1b',
    Home: '\x1b[H', End: '\x1b[F', PageUp: '\x1b[5~', PageDown: '\x1b[6~', Delete: '\x1b[3~', Insert: '\x1b[2~',
    F1: '\x1bOP', F2: '\x1bOQ', F3: '\x1bOR', F4: '\x1bOS', F5: '\x1b[15~', F6: '\x1b[17~', F7: '\x1b[18~',
    F8: '\x1b[19~', F9: '\x1b[20~', F10: '\x1b[21~', F11: '\x1b[23~', F12: '\x1b[24~' };
  if (map[e.key]) return (e.altKey && e.key.length > 1 && !arrows[e.key] ? '\x1b' : '') + map[e.key];
  if (e.ctrlKey && !e.altKey && !e.metaKey && e.key.length === 1) {
    const c = e.key.toUpperCase().charCodeAt(0);
    if (c >= 64 && c <= 95) return String.fromCharCode(c - 64);
    if (e.key === ' ') return '\x00';
    if (e.key === '/') return '\x1f';
  }
  if (e.altKey && !e.ctrlKey && e.key.length === 1) return '\x1b' + e.key;
  return null;
}
ta.addEventListener('keydown', e => {
  if (e.isComposing || e.keyCode === 229) return;
  if ((e.ctrlKey || e.metaKey) && (e.key === 'v' || e.key === 'V')) return;  // -> paste event
  if ((e.ctrlKey || e.metaKey) && e.key === 'c' && getSelection().toString()) return;  // copy selection
  let s = keySeq(e);
  if (s === null && e.key.length === 1 && !e.ctrlKey && !e.metaKey) s = applyMods(e.key);
  else if (s !== null && (mods.ctrl || mods.alt) && e.key.length === 1) s = applyMods(e.key);
  if (s !== null) { e.preventDefault(); sendInput(s); ta.value = SENT; prev = SENT; }
});
// Soft keyboards: diff the textarea against a sentinel (like the trackpad
// field) so each keystroke is sent at once, even inside an IME composition
// (GBoard composes every word); corrections arrive as DEL + new text.
const SENT = String.fromCharCode(0x200B), SENT_RE = new RegExp(SENT, 'g');
let prev = SENT;
ta.value = SENT;
ta.addEventListener('input', e => {
  const raw = ta.value;
  const nl = raw.indexOf('\n');
  const cur = nl >= 0 ? raw.slice(0, nl) : raw;
  let p = 0;
  while (p < prev.length && p < cur.length && prev[p] === cur[p]) p++;
  let del = prev.length - p;
  if (p === 0 && del > 1 && prev.startsWith(SENT)) del--;  // sentinel went with real text
  const ins = cur.slice(p).replace(SENT_RE, '');
  if (del) sendInput('\x7f'.repeat(del));
  if (ins) sendInput(ins.length === 1 ? applyMods(ins) : ins);
  if (nl >= 0) { sendInput('\r'); ta.value = SENT; prev = SENT; return; }
  prev = SENT + cur.replace(SENT_RE, '');
  if (!cur.startsWith(SENT)) ta.value = prev;
  if (!e.isComposing && prev.length > 64) { ta.value = SENT; prev = SENT; }
});
ta.addEventListener('compositionend', () => {
  if (ta.value.length > 64) { ta.value = SENT; prev = SENT; }
});
ta.addEventListener('paste', e => {
  e.preventDefault();
  paste((e.clipboardData || window.clipboardData).getData('text'));
});
function paste(text) {
  if (!text) return;
  text = text.replace(/\r?\n/g, '\r');
  sendInput(emu?.modes.paste ? `\x1b[200~${text}\x1b[201~` : text);
}
ta.addEventListener('focus', () => { screenEl.classList.add('focused'); schedule(); });
ta.addEventListener('blur', () => { screenEl.classList.remove('focused'); schedule(); });
// A tap (not a scroll or a text selection) focuses the terminal. `click`
// is also what iOS requires before it will raise the keyboard.
let downAt = null;
screenEl.addEventListener('pointerdown', e => { downAt = [e.clientX, e.clientY]; });
screenEl.addEventListener('click', e => {
  if (downAt && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 8) return;
  if (getSelection().toString()) return;
  ta.focus({ preventScroll: true });
});
screenEl.addEventListener('focus', () => ta.focus({ preventScroll: true }));

// accessory bar (mobile keys)
const ACC = [['esc', 'Esc', '\x1b'], ['tab', 'Tab', '\t'], ['ctrl', 'Ctrl'], ['alt', 'Alt'], ['up', '↑', null, 'ArrowUp'],
  ['down', '↓', null, 'ArrowDown'], ['left', '←', null, 'ArrowLeft'], ['right', '→', null, 'ArrowRight'],
  ['^c', '^C', '\x03'], ['^d', '^D', '\x04'], ['^z', '^Z', '\x1a'], ['^l', '^L', '\x0c'], ['^r', '^R', '\x12'],
  ['|', '|', '|'], ['~', '~', '~'], ['/', '/', '/'], ['\\', '\\', '\\'], ['-', '-', '-'], ['home', 'Home', '\x1b[H'],
  ['end', 'End', '\x1b[F'], ['pgup', 'PgUp', '\x1b[5~'], ['pgdn', 'PgDn', '\x1b[6~'], ['paste', 'Paste']];
function renderAcc() {
  const bar = $('term-acc');
  bar.replaceChildren(...ACC.map(([id, label, seq, key]) => {
    const on = mods[id];
    return h('button', { class: 'chip' + (id === 'ctrl' || id === 'alt' ? ' mod' + (on ? ' on' : '') : ''),
      onpointerdown: e => e.preventDefault(),  // keep the keyboard open
      onclick: async () => {
        haptic(5);
        if (id === 'ctrl' || id === 'alt') { mods[id] = !mods[id]; renderAcc(); return; }
        if (id === 'paste') {
          try { paste(await navigator.clipboard.readText()); } catch { toast('Long-press the terminal and choose Paste', { ms: 2500 }); }
          return;
        }
        if (key) { sendInput(keySeq({ key, ctrlKey: mods.ctrl, altKey: mods.alt, shiftKey: false })); if (mods.ctrl || mods.alt) { mods.ctrl = mods.alt = false; renderAcc(); } return; }
        sendInput(seq.length === 1 && /[|~/\\-]/.test(seq) ? applyMods(seq) : seq);
      } }, label);
  }));
}

// ----------------------------------------------------------- toolbar ---
function renderShells() {
  const seg = $('term-shells');
  const shells = INFO.shells?.length ? INFO.shells : [{ id: 'ps', name: 'PowerShell' }];
  if (!shells.some(s => s.id === shell)) shell = shells[0].id;
  seg.replaceChildren(...shells.map(s => h('button', { class: s.id === shell ? 'on' : '', onclick: () => {
    if (s.id === shell) return;
    haptic(5);
    shell = s.id;
    setSetting('shell', shell);
    renderShells();
    connect();
  } }, s.name.replace('Command Prompt', 'CMD').replace('PowerShell 7', 'pwsh'))));
}
function font(d) {
  setSetting('termFont', Math.min(22, Math.max(9, settings.termFont + d)));
  measure();
  onResize();
}
$('term-font-up').addEventListener('click', () => { haptic(5); font(1); });
$('term-font-dn').addEventListener('click', () => { haptic(5); font(-1); });
$('term-menu').addEventListener('click', async () => {
  const v = await choose('Terminal', [
    { label: 'Paste from clipboard', value: 'paste' }, { label: 'Copy all output', value: 'copy' },
    { label: 'Clear scrollback', value: 'clear' }, { label: 'Restart shell', value: 'restart' },
    { label: 'End session', value: 'kill', red: true }]);
  if (!v || !emu) return;
  if (v === 'paste') { try { paste(await navigator.clipboard.readText()); } catch { toast('Clipboard not available here', { err: true }); } }
  else if (v === 'copy') {
    const text = [...emu.scrollback, ...emu.buf].map(l => l.ch.join('').replace(/\s+$/, '')).join('\n').replace(/\n+$/, '');
    toast((await copyToDevice(text)) ? 'Copied' : 'Copy failed', { ic: 'check' });
  } else if (v === 'clear') { emu.scrollback = []; buildDom(); }
  else if (v === 'restart') connect(true);
  else if (v === 'kill') ws?.readyState === 1 && ws.send(JSON.stringify({ t: 'kill' }));
});

// ---------------------------------------------------------- lifecycle ---
let inited = false;
export function show() {
  active = true;
  if (!inited) {
    inited = true;
    renderShells();
    renderAcc();
  }
  if (!ws) connect();  // builds the emulator once the grid is measurable
  requestAnimationFrame(onResize);
  if (matchMedia('(pointer: fine)').matches) ta.focus({ preventScroll: true });
}
export function hide() { active = false; }  // the session keeps running; the socket stays open
document.addEventListener('visibilitychange', () => { if (active && !document.hidden && !ws) connect(); });
hydrateIcons(document.getElementById('view-term'));
