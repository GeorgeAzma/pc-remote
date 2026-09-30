// Controls tab: quick actions, sliders, radios, stream, tools and power,
// rendered from the server's @command registry (unknown commands get a
// generic row).
import { api, run, url, h, ico, toast, haptic, sheet, choose, Slider, toggle, twoTap, INFO, fmtBytes, fmtDuration,
         copyToDevice, tabSwitch, showTab, setToken } from './app.js';
import { qrSvg } from './qr.js';
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
  wifi: 'var(--tint)', bluetooth: 'var(--indigo)', power: 'var(--red)', info: 'var(--gray)', shield: 'var(--green)', lock: 'var(--teal)',
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

// The app's own icon, from the PC (lazily, as rows scroll into view); a
// symbol (or nothing) when it has none.
function appIcon(q, fallback) {
  const img = h('img', { class: 'app-ic', src: url('/appicon', q), loading: 'lazy', alt: '', decoding: 'async' });
  img.addEventListener('error', () => img.replaceWith(fallback ? ico(fallback, 'ic') : h('span', { class: 'app-ic' })), { once: true });
  return img;
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
    h('div', { class: 'ctl-head' }, h('div', {}, h('h1', {}, INFO.host || 'PC'),
      h('button', { class: 'sub', id: 'ctl-stats', title: 'Open the System tab', onclick: () => { haptic(5); showTab('monitor'); } })), tabSwitch()),
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
      row('shield', 'Sign-in & devices', 'Add a device · code or password · on / off', securitySheet),
      INFO.https ? row('lock', 'Secure connection', secureNow() ? 'On (HTTPS)' : 'Install the certificate for the HD stream', certSheet) : null,
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

// --------------------------------------------------------------- about ---
const kv = (k, v, extra) => v == null || v === '' ? null
  : h('div', { class: 'kv' }, h('span', { class: 'k' }, k), h('span', { class: 'v' }, v), extra || null);
const copyBtn = (text, label = 'Copy') => h('button', { class: 'kv-copy', title: text, onclick: async e => {
  e.stopPropagation(); haptic(5); toast((await copyToDevice(text)) ? 'Copied ' + text : 'Copy failed', { ic: 'check', ms: 1400 });
} }, label);
const netName = ip => /^192\.168\.|^10\./.test(ip) ? 'Local network'
  : /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(ip) ? 'Tailscale'
  : 'Other';  // VPNs, WSL / Hyper-V adapters, …
const NETS = ['Local network', 'Tailscale', 'Other'];
const byNet = ips => [...ips].sort((a, b) => NETS.indexOf(netName(a)) - NETS.indexOf(netName(b)));
const when = t => new Date(t * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });

async function aboutSheet() {
  const s = sheet({ title: 'About this PC', body: h('div', { class: 'empty' }, 'Loading…') });
  try { about = await run('about'); } catch (e) { s.setBody(h('div', { class: 'empty' }, e.message)); return; }
  const a = about, port = location.port || (location.protocol === 'https:' ? 443 : 80);
  const plan = resolvePlan(), method = METHODS.find(m => m.id === plan.mode)?.name;
  const ips = byNet(a.ips);
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
      kv('Sign-in', a.sign_in ? 'Required' : 'Off — anyone who can reach this PC can use it'),
      kv('Video encoder', [...(a.encoders.h264 || []), ...(a.encoders.hevc || [])].join(', ') || 'None found'),
      kv('JPEG', a.tiles ? 'Changed areas only' : 'Whole frames (install numpy, simplejpeg)'),
      kv('ffmpeg', a.ffmpeg || 'Not found'), kv('Python', a.python))));
}

// ------------------------------------------------------ sign-in & pairing ---
async function securitySheet() {
  const s = sheet({ title: 'Sign-in & devices', body: h('div', { class: 'empty' }, 'Loading…') });
  let st;
  try { st = await api('/api/security'); } catch (e) { s.setBody(h('div', { class: 'empty' }, e.message)); return; }
  const save = async patch => {
    try { st = await api('/api/security', patch); } catch (e) { toast(e.message, { err: true }); return false; }
    if (patch.new_key) setToken(st.key);  // this device stays signed in
    draw();
    return true;
  };
  const passwordSheet = () => {
    const a = h('input', { class: 'input', type: 'password', placeholder: 'New password (6+ characters)', autocomplete: 'new-password' });
    const b = h('input', { class: 'input', type: 'password', placeholder: 'Again', autocomplete: 'new-password' });
    const p = sheet({ title: 'Your password', body: h('div', { class: 'form' }, a, b,
      h('button', { class: 'btn', onclick: async () => {
        if (a.value !== b.value) { toast('The two don\u2019t match', { err: true }); return; }
        if (await save({ password: a.value })) { toast('Password saved', { ic: 'check' }); p.close(); }
      } }, 'Save')) });
    setTimeout(() => a.focus(), 300);
  };
  const draw = () => {
    const on = toggle(st.enabled, async v => { if (!(await save({ enabled: v }))) on.input.checked = !v; });
    s.setBody(h('div', {},
      h('div', { class: 'card' }, row('shield', 'Require sign-in',
        st.enabled ? 'A new device needs the code or password once' : 'Off: anyone who can reach this PC can use it', null, { right: on })),
      st.enabled ? h('div', { class: 'group-title' }, st.code ? 'Access code' : 'Password') : null,
      st.enabled ? h('div', { class: 'card' },
        st.code ? h('div', { class: 'kv' }, h('span', { class: 'code' }, st.code), copyBtn(st.code)) : kv('Password', 'Your own'),
        row('lock', st.own_password ? 'Change password' : 'Use my own password', null, passwordSheet),
        st.own_password ? row('restart', 'Use a generated code instead', null, () => save({ new_code: true })) : null) : null,
      h('div', { class: 'group-title' }, 'Devices'),
      h('div', { class: 'card' },
        row('download', 'Add a device', 'Scan a QR code: signs it in, from anywhere', () => pairSheet(st)),
        st.enabled ? row('logout', 'Sign out all other devices', 'They\u2019ll need the code or password again', () => save({ new_key: true }),
                         { strong: true }) : null),
      st.env_token ? h('div', { class: 'foot', style: 'text-align:left;margin:10px 4px 0' }, 'PC_API_TOKEN is set as well, and also lets devices in.') : null));
    hydrateIcons(s.el);
  };
  draw();
}

// A QR code a new device scans: the address and, with sign-in on, the key.
function pairSheet(st) {
  let ips = byNet(st.ips);
  // WSL / Hyper-V adapters are unreachable from a phone: only offer others if there's nothing better
  if (ips.some(ip => netName(ip) !== 'Other')) ips = ips.filter(ip => netName(ip) !== 'Other');
  if (!ips.length) ips = [location.hostname];
  let pick = ips[0];
  const seg = h('div', { class: 'seg' }), qr = h('div', { class: 'qr' }), link = h('div', { class: 'foot', style: 'margin:0;word-break:break-all' });
  const address = () => `http://${pick}:${st.port}/`;
  const signedLink = () => address() + (st.enabled ? `?token=${st.key}` : '');
  const draw = () => {
    seg.replaceChildren(...ips.map(ip => h('button', { class: ip === pick ? 'on' : '', onclick: () => { pick = ip; haptic(5); draw(); } },
      netName(ip) === 'Other' ? ip : netName(ip))));
    qr.innerHTML = qrSvg(signedLink());
    link.replaceChildren(address(), st.enabled && st.code ? ` · code ${st.code}` : '');
  };
  draw();
  sheet({ title: 'Add a device', body: h('div', { class: 'form' },
    h('div', { class: 'note' }, st.enabled
      ? 'Scan this with the phone\u2019s camera, or with Scan the QR code on the app\u2019s sign-in screen: it signs in. Or open the address below and enter the code. For devices away from home, pick Tailscale.'
      : 'Scan this with the phone\u2019s camera to open PC Remote (sign-in is off).'),
    h('div', { style: 'overflow-x:auto;display:flex;justify-content:center' }, seg), qr, link,
    h('button', { class: 'btn gray', onclick: async () => toast((await copyToDevice(signedLink()))
      ? (st.enabled ? 'Copied: the link signs in whoever opens it' : 'Copied') : 'Copy failed', { ic: 'check' }) }, 'Copy the link')) });
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
    list.replaceChildren(...(hits.length ? hits.map(a => h('button', { class: 'list-row', onclick: () => go(a) }, appIcon({ app: a }, 'rocket'), h('span', { class: 'nm' }, a)))
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
      appIcon({ pid: p.pids[0], name: p.name }),
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
export async function welcome() {
  try { pairSheet(await api('/api/security')); } catch { aboutSheet(); }
}
export function show() {
  active = true;
  if (!loaded) { loaded = true; load(); } else api('/api/state').then(d => { state = d.state; pending = d.pending; render(); }).catch(() => {});
  clearTimeout(statsT);
  pollStats();
}
export function hide() { active = false; clearTimeout(statsT); }
