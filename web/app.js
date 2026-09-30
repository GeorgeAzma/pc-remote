// App shell: auth, settings, tabs, sheets, toasts and shared UI helpers.
import { icon, hydrateIcons } from './icons.js';
export { icon };

// ---------------------------------------------------------------- auth ---
const qs = new URLSearchParams(location.search);
let TOKEN = qs.get('token') || store('token') || '';
if (qs.has('token')) {
  store('token', TOKEN);
  history.replaceState(null, '', location.pathname);  // keep the secret out of the address bar
}
function store(k, v) {
  try {
    if (v === undefined) return localStorage.getItem('pc.' + k);
    localStorage.setItem('pc.' + k, typeof v === 'string' ? v : JSON.stringify(v));
  } catch { return null; }
}
export function url(path, q = {}) {
  const u = new URL(path, location.origin);
  if (TOKEN) u.searchParams.set('token', TOKEN);
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null) u.searchParams.set(k, v);
  return u.toString();
}
export function wsUrl(path, q = {}) {
  return url(path, q).replace(/^http/, 'ws');
}
export async function api(path, body) {
  const res = await fetch(url(path), body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (res.status === 401) { askToken(); throw new Error('Token required'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
export async function run(cmd, args = {}) {
  return (await api('/' + cmd, args)).result;
}
let asking = false;
function askToken() {
  if (asking) return;
  asking = true;
  const inp = h('input', { class: 'input', type: 'password', placeholder: 'PC_API_TOKEN', autocomplete: 'current-password' });
  const s = sheet({ title: 'Token required', body: h('div', { class: 'form' },
    h('div', { style: 'color:var(--label2);font-size:14px' }, 'This PC requires the token set in launch_remote.bat.'),
    inp, h('button', { class: 'btn', onclick: () => { store('token', inp.value.trim()); location.reload(); } }, 'Continue')),
    onClose: () => { asking = false; } });
  setTimeout(() => inp.focus(), 300);
  return s;
}

// ------------------------------------------------------------ settings ---
const DEFAULTS = {
  speed: 1, accel: 0.6, scroll: 1, natural: true, touch: 'direct', stream: 'auto', quality: 0.5,
  stats: false, haptics: true, termFont: 13, tab: 'remote', recents: [], shell: 'ps',
  hidePanel: false, panelW: 0, padH: 0,  // 0 = automatic size
  maxMbps: 0,  // stream bitrate limit, 0 = none
  pins: [],  // shortcuts shown in the key bar
};
export const settings = { ...DEFAULTS, ...(JSON.parse(store('settings') || '{}')) };
delete settings.fps;    // superseded by the Speed <-> Quality slider (+ display refresh)
delete settings.maxbr;  // bitrate follows the measured link
if ('hidePad' in settings) { settings.hidePanel = settings.hidePad; delete settings.hidePad; }
if ('codec' in settings) {  // pre-"stream" setting name
  if (settings.codec !== 'auto') settings.stream = settings.codec;
  delete settings.codec;
}
export const bus = new EventTarget();
export function setSetting(k, v) {
  settings[k] = v;
  store('settings', settings);
  bus.dispatchEvent(new CustomEvent('settings', { detail: { key: k, value: v } }));
}

// ------------------------------------------------------------- helpers ---
export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'icon') el.innerHTML = icon(v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
    el.append(kid instanceof Node ? kid : document.createTextNode(kid));
  return el;
}
export const ico = (name, cls = '') => h('span', { class: cls, 'data-icon': name, icon: name });
export function haptic(ms = 8) {
  if (settings.haptics && navigator.vibrate) try { navigator.vibrate(ms); } catch {}
}
export function fmtBytes(n) {
  if (n == null) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + ' ' + u[i];
}
export function fmtDuration(s) {
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  if (d) return `${d}d ${hh}h`;
  if (hh) return `${hh}h ${m}m`;
  return m ? `${m}:${String(ss).padStart(2, '0')}` : `${ss}s`;
}

let toastT = 0;
export function toast(msg, { err = false, ic = null, ms = 1800 } = {}) {
  const t = document.getElementById('toast');
  t.className = 'toast show' + (err ? ' err' : '');
  t.replaceChildren(...(ic ? [ico(ic)] : []), h('span', {}, msg));
  clearTimeout(toastT);
  toastT = setTimeout(() => t.classList.remove('show'), err ? Math.max(ms, 3200) : ms);
}

export async function copyToDevice(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch {}
  const ta = h('textarea', { style: 'position:fixed;top:-100px;opacity:0' });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch {}
  ta.remove();
  return ok;
}

// -------------------------------------------------------------- sheets ---
const openSheets = [];
export function sheet({ title = '', body, onClose, wide = false } = {}) {
  const root = document.getElementById('sheets');
  const bodyEl = h('div', { class: 'sheet-body' }, body);
  const el = h('div', { class: 'sheet-wrap' },
    h('div', { class: 'backdrop', onclick: () => close() }),
    h('div', { class: 'sheet' + (wide ? ' wide' : '') },
      h('div', { class: 'grabber' }),
      title !== null ? h('div', { class: 'sheet-head' }, h('h2', {}, title),
        h('button', { class: 'close', 'aria-label': 'Close', onclick: () => close(), icon: 'x' })) : null,
      bodyEl));
  root.append(el);
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('open')));
  let closed = false;
  // Swipe down on the grabber/header to dismiss.
  const sh = el.querySelector('.sheet');
  let y0 = null;
  sh.addEventListener('touchstart', e => { if (e.target.closest('.grabber,.sheet-head')) y0 = e.touches[0].clientY; }, { passive: true });
  sh.addEventListener('touchmove', e => {
    if (y0 === null) return;
    const dy = Math.max(0, e.touches[0].clientY - y0);
    sh.style.transition = 'none';
    sh.style.transform = `translateY(${dy}px)`;
  }, { passive: true });
  sh.addEventListener('touchend', e => {
    if (y0 === null) return;
    const dy = e.changedTouches[0].clientY - y0;
    y0 = null;
    sh.style.transition = '';
    sh.style.transform = '';
    if (dy > 90) close();
  });
  function close() {
    if (closed) return;
    closed = true;
    el.classList.remove('open');
    el.querySelectorAll('.backdrop, .sheet').forEach(n => { n.style.pointerEvents = 'none'; });  // taps pass through while it animates out
    openSheets.splice(openSheets.indexOf(api_), 1);
    setTimeout(() => el.remove(), 320);
    onClose && onClose();
  }
  const api_ = { el, body: bodyEl, close, setBody: b => bodyEl.replaceChildren(b) };
  openSheets.push(api_);
  hydrateIcons(el);
  return api_;
}
addEventListener('keydown', e => { if (e.key === 'Escape' && openSheets.length) openSheets.at(-1).close(); });

/** iOS action sheet. options: [{label, value, red}] -> Promise<value|null> */
export function choose(title, options, message = '') {
  return new Promise(resolve => {
    let done = false;
    const s = sheet({ title, onClose: () => { if (!done) resolve(null); }, body: h('div', {},
      message ? h('div', { style: 'color:var(--label2);font-size:14px;margin:0 2px 12px' }, message) : null,
      h('div', { class: 'card action-list' }, options.map(o => h('button', {
        class: 'list-row' + (o.red ? ' red' : ''),
        onclick: () => { done = true; resolve(o.value); s.close(); } }, o.label)))) });
  });
}

// -------------------------------------------------------------- slider ---
export class Slider {
  constructor({ ic, label = '', value = 50, min = 0, max = 100, step = 1, thin = false, format, onInput, onChange }) {
    this.min = min; this.max = max; this.step = step;
    this.format = format || (v => Math.round(v) + (max === 100 ? '%' : ''));
    this.onInput = onInput; this.onChange = onChange;
    this.fill = h('div', { class: 'fill' });
    this.val = h('span', { class: 'val' });
    this.el = h('div', { class: 'slider' + (thin ? ' thin' : ''), role: 'slider', tabindex: '0', 'aria-label': label,
      'aria-valuemin': min, 'aria-valuemax': max },
      this.fill, h('div', { class: 'lbl' }, ic ? ico(ic) : null, label ? h('span', {}, label) : null, this.val));
    this.set(value);
    let active = false;
    const at = e => {
      const r = this.el.getBoundingClientRect();
      return this.min + Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * (this.max - this.min);
    };
    this.el.addEventListener('pointerdown', e => {
      active = true; this.el.setPointerCapture(e.pointerId); this._input(at(e));
    });
    this.el.addEventListener('pointermove', e => { if (active) this._input(at(e)); });
    const end = () => { if (active) { active = false; this.onChange && this.onChange(this.value); } };
    this.el.addEventListener('pointerup', end);
    this.el.addEventListener('pointercancel', end);
    this.el.addEventListener('keydown', e => {
      const d = e.key === 'ArrowRight' || e.key === 'ArrowUp' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowDown' ? -1 : 0;
      // One detent per press on coarse sliders, 5% jumps on fine ones.
      const stride = (this.max - this.min) / this.step <= 10 ? 1 : 5;
      if (d) { e.preventDefault(); this._input(this.value + d * this.step * stride); end(); }
    });
  }
  _input(v) {
    const q = Math.round(v / this.step) * this.step;
    if (q === this.value) return;
    const crossed = [this.min, this.max].includes(q);
    this.set(q);
    if (crossed) haptic(6);
    this.onInput && this.onInput(this.value);
  }
  set(v) {
    this.value = Math.min(this.max, Math.max(this.min, v));
    this.fill.style.width = (100 * (this.value - this.min) / (this.max - this.min)) + '%';
    this.val.textContent = this.format(this.value);
    this.el.setAttribute('aria-valuenow', this.value);
  }
}

// ------------------------------------------------------ two-tap confirm ---
// Consequential buttons arm on the first tap (turn red, label swaps, a bar
// drains) and only act on a second tap within `ms`. Tapping anywhere else
// disarms. onLong (press and hold) opens extras such as timers.
let armedEl = null;
function disarm(el) {
  if (!el) return;
  el.classList.remove('arm');
  clearTimeout(el._armT);
  if (el._armLabel) { el._armLabel.textContent = el._armText; el._armLabel = null; }
  if (armedEl === el) armedEl = null;
}
export function twoTap(el, run, { confirm = 'Tap again', ms = 3000, onLong = null } = {}) {
  let longT = 0, longFired = false;
  if (onLong) {
    el.addEventListener('pointerdown', () => {
      longFired = false;
      longT = setTimeout(() => { longFired = true; disarm(el); haptic(15); onLong(); }, 500);
    });
    for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) el.addEventListener(ev, () => clearTimeout(longT));
    el.addEventListener('contextmenu', e => e.preventDefault());
  }
  el.addEventListener('click', async e => {
    e.stopPropagation();
    if (longFired) { longFired = false; return; }
    if (!el.classList.contains('arm')) {
      if (armedEl) disarm(armedEl);
      armedEl = el;
      el.style.setProperty('--arm-ms', ms + 'ms');
      el.classList.add('arm');
      const lab = el.querySelector('[data-label]');
      if (lab) { el._armLabel = lab; el._armText = lab.textContent; lab.textContent = confirm; }
      haptic(12);
      el._armT = setTimeout(() => disarm(el), ms);
      return;
    }
    disarm(el);
    haptic(22);
    await run();
  });
  return el;
}
document.addEventListener('pointerdown', e => { if (armedEl && !armedEl.contains(e.target)) disarm(armedEl); }, true);

export function toggle(checked, onchange) {
  const inp = h('input', { type: 'checkbox', role: 'switch' });
  inp.checked = !!checked;
  inp.addEventListener('change', () => { haptic(6); onchange(inp.checked); });
  const el = h('label', { class: 'switch' }, inp, h('i'));
  el.input = inp;
  return el;
}

// ---------------------------------------------------------------- tabs ---
const views = {};
export function registerView(name, mod) { views[name] = mod; }
let current = null;
export function showTab(name) {
  if (!views[name]) name = 'remote';
  if (current === name) return;
  const prev = current;
  current = name;
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.dataset.view === name));
  document.querySelectorAll('.tabsw button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  if (prev && views[prev].hide) views[prev].hide();
  views[name].show && views[name].show();
  setSetting('tab', name);
}

// Switching views: a small segmented control each view keeps in its own top
// bar (over the picture, the terminal's bar, the Controls header), so it
// takes no room of its own.
const TABS = [['remote', 'trackpad', 'Remote'], ['term', 'terminal', 'Terminal'], ['controls', 'controls', 'Controls'],
              ['monitor', 'pulse', 'System']];
export function tabSwitch(el = h('div', { class: 'tabsw' })) {
  el.setAttribute('role', 'tablist');
  el.replaceChildren(...TABS.map(([t, ic, name]) => h('button', {
    'data-tab': t, class: t === current ? 'on' : '', role: 'tab', 'aria-label': name, title: name, icon: ic,
    onclick: () => { haptic(5); showTab(t); } })));
  return el;
}
export const currentTab = () => current;

// ------------------------------------------------------------ viewport ---
// iOS keeps the layout viewport when the keyboard opens; size the shell to
// the *visual* viewport so inputs stay above the keyboard.
function fitViewport() {
  const vv = window.visualViewport;
  const hgt = (vv && vv.height) || innerHeight;
  if (!hgt) return;  // not laid out (page restored in the background)
  document.documentElement.style.setProperty('--app-h', hgt + 'px');
  const ae = document.activeElement;
  const typing = ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA') && !ae.closest('.sheet');
  document.body.classList.toggle('kb-open', !!(typing && vv && screen.height - vv.height > 220 && innerHeight - vv.height > 80));
  if (vv && vv.offsetTop) window.scrollTo(0, 0);
  bus.dispatchEvent(new Event('layout'));
}
window.visualViewport?.addEventListener('resize', fitViewport);
window.visualViewport?.addEventListener('scroll', fitViewport);
addEventListener('resize', fitViewport);
addEventListener('focusin', () => setTimeout(fitViewport, 50));
addEventListener('focusout', () => setTimeout(fitViewport, 50));

// ---------------------------------------------------------------- boot ---
export let INFO = { shells: [], displays: [], video: {} };
async function boot() {
  hydrateIcons();
  fitViewport();
  document.querySelectorAll('[data-tabs]').forEach(el => tabSwitch(el));
  try { INFO = await api('/api/info'); } catch (e) { if (!/Token/.test(e.message)) toast('Server unreachable', { err: true }); }
  document.title = (INFO.host || 'PC') + ' · Remote';
  const [remote, term, controls, monitor] = await Promise.all([import('./remote.js'), import('./term.js'), import('./controls.js'),
                                                            import('./monitor.js')]);
  registerView('remote', remote);
  registerView('term', term);
  registerView('controls', controls);
  registerView('monitor', monitor);
  // Opened from the PC (Start menu, or right after installing): show its
  // addresses to open on a phone, not a picture of its own screen.
  if (new URLSearchParams(location.search).has('welcome')) {
    history.replaceState(null, '', location.pathname);
    showTab('controls');
    controls.welcome();
  } else showTab(settings.tab);
}
boot();
