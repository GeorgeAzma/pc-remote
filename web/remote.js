// Remote tab: live screen (WebCodecs H.264 or MJPEG), cursor overlay with
// local prediction, trackpad + direct-touch gestures, live keyboard.
import { settings, setSetting, bus, h, ico, toast, haptic, sheet, url, wsUrl, INFO, Slider, toggle,
         copyToDevice } from './app.js';
import { hydrateIcons } from './icons.js';

const $ = id => document.getElementById(id);
const stage = $('stage'), inner = $('stage-inner'), canvas = $('screen'), cursorEl = $('cursor');
const pad = $('pad'), kb = $('kb'), strip = $('keystrip');
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

let displays = INFO.displays?.length ? INFO.displays : [{ x: 0, y: 0, w: 1920, h: 1080, hz: 60, name: 'Display 1' }];
let dispIdx = 0;
const disp = () => displays[Math.min(dispIdx, displays.length - 1)];

// =============================================================== input ===
// One WebSocket carries input up and cursor/clipboard state down.
const input = {
  ws: null, seq: 0, queue: [], retry: 0,
  connect() {
    if (this.ws && this.ws.readyState <= 1) return;
    if (this.ws) this.ws.onclose = null;  // CLOSING: let it go, don't let it null the new one
    const ws = this.ws = new WebSocket(wsUrl('/ws'));
    ws.onopen = () => { this.retry = 0; for (const m of this.queue.splice(0)) ws.send(JSON.stringify(m)); };
    ws.onmessage = e => onInputMsg(JSON.parse(e.data));
    ws.onclose = () => {
      if (this.ws !== ws) return;  // an older socket finishing its close
      this.ws = null;
      if (active) setTimeout(() => this.connect(), Math.min(3000, 300 * 2 ** this.retry++));
    };
  },
  send(m) {
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(m));
    else {
      // Relative moves queued while reconnecting are summed, not replayed.
      const last = this.queue.at(-1);
      if (m.t === 'mv' && last?.t === 'mv') { last.dx += m.dx; last.dy += m.dy; last.s = m.s; }
      else this.queue.push(m);
      this.connect();
    }
  },
};
setInterval(() => input.ws?.readyState === 1 && input.send({ t: 'hb' }), 15000);

// ---- cursor overlay with client-side prediction ----
const cur = { x: 0, y: 0, v: false, id: 0, pending: [], shapes: new Map(), lastLocal: 0 };
function onInputMsg(m) {
  if (m.t === 'cur') {
    if (m.s) cur.pending = cur.pending.filter(p => p.s > m.s);
    else if (performance.now() - cur.lastLocal < 120 && cur.pending.length) return; // our own echo is coming
    cur.x = m.x; cur.y = m.y; cur.v = m.v; cur.id = m.id;
    drawCursor();
  } else if (m.t === 'shape') {
    const img = new Image();
    img.src = 'data:image/png;base64,' + m.png;
    cur.shapes.set(m.id, { img, w: m.w, h: m.h, hx: m.hx, hy: m.hy });
    drawCursor();
  } else if (m.t === 'clip') {
    showClip(m.text);
  } else if (m.t === 'hi') {
    displays = m.displays;
    updateDisplayPill();
    layout();
  } else if (m.t === 'err') {
    toast(m.msg, { err: true });
  }
}
function predicted() {
  const d = disp();
  let x = cur.x, y = cur.y;
  for (const p of cur.pending) { x += p.dx; y += p.dy; }
  return [clamp(x, d.x, d.x + d.w - 1), clamp(y, d.y, d.y + d.h - 1)];
}
let lastShape = null;
function drawCursor() {
  const d = disp(), shp = cur.shapes.get(cur.id);
  const [x, y] = predicted();
  const inside = x >= d.x && y >= d.y && x < d.x + d.w && y < d.y + d.h;
  if (!cur.v || !shp || !inside || !stageBox.w) { cursorEl.style.opacity = 0; return; }
  if (lastShape !== shp) { cursorEl.src = shp.img.src; lastShape = shp; }
  const k = stageBox.w / d.w;              // css px per PC px (unzoomed)
  const sx = (x - d.x) * k * view.s + view.tx, sy = (y - d.y) * k * view.s + view.ty;
  // Scale with the picture, but never below a finger-legible size.
  const s = Math.max(k * view.s, 0.58);
  cursorEl.style.width = shp.w * s + 'px';
  cursorEl.style.height = shp.h * s + 'px';
  cursorEl.style.transform = `translate(${sx - shp.hx * s}px, ${sy - shp.hy * s}px)`;
  cursorEl.style.opacity = 1;
  if (view.s > 1.01 && followCursor) keepInView(sx, sy);
}

// ---- pointer output helpers ----
let followCursor = false;
function moveBy(dx, dy) {
  const s = ++input.seq;
  cur.pending.push({ s, dx, dy });
  cur.lastLocal = performance.now();
  input.send({ t: 'mv', dx, dy, s });
  followCursor = true;
  drawCursor();
}
function moveTo(x, y) {
  const s = ++input.seq;
  cur.pending = [];
  cur.x = x; cur.y = y;
  cur.lastLocal = performance.now();
  input.send({ t: 'abs', x, y, s });
  followCursor = false;
  drawCursor();
}
function withMods(fn) {
  const m = activeMods();
  if (!m.length) return fn();
  for (const k of m) input.send({ t: 'key', code: MOD_CODE[k], d: 1 });
  fn();
  for (const k of m.reverse()) input.send({ t: 'key', code: MOD_CODE[k], d: 0 });
  consumeMods();
}
const click = (b = 'left', n = 1) => withMods(() => input.send({ t: 'click', b, n }));
const button = (b, d) => input.send({ t: 'btn', b, d });
const wheel = (dy, dx) => input.send({ t: 'wheel', dy, dx });

// Velocity-dependent pointer gain (like a real trackpad): slow strokes are
// precise (~2 screen px per finger px), flicks cross the screen in one go.
function gain(v) {
  const t = Math.pow(clamp((v - 0.03) / 1.2, 0, 1), 0.85);
  const size = clamp(disp().w / 2560, 0.6, 2);
  return settings.speed * size * (2 + 20 * settings.accel * t);
}

// ============================================================ gestures ===
// A small recognizer over Pointer Events, shared by the pad and the screen.
function gestures(el, { direct }) {
  const pts = new Map();
  let mode = null, lpTimer = 0, t0 = 0, moved = false, lastTapAt = 0, tapDrag = false;
  let vel = 0, lastT = 0, pinch0 = null, scroll = { vx: 0, vy: 0, t: 0 };
  const SLOP = 7, LONG = 380, TAP = 260;
  // The recognizer owns the button it pressed and always releases it when
  // the last finger lifts, whatever mode the gesture ended up in.
  let held = false;
  const press = () => { held = true; button('left', true); };
  const release = () => { if (held) { held = false; button('left', false); } pad.classList.remove('dragging'); };

  const toPc = (cx, cy) => {  // stage css point -> PC pixel
    const r = stage.getBoundingClientRect(), d = disp();
    const k = stageBox.w / d.w;
    return [d.x + ((cx - r.left - view.tx) / view.s) / k, d.y + ((cy - r.top - view.ty) / view.s) / k];
  };
  const isDirect = () => direct && settings.touch === 'direct';
  const centroid = () => {
    let x = 0, y = 0;
    for (const p of pts.values()) { x += p.x; y += p.y; }
    return [x / pts.size, y / pts.size];
  };
  const spread = () => {
    const [a, b] = [...pts.values()];
    return Math.hypot(a.x - b.x, a.y - b.y);
  };
  function ripple(x, y, right) {
    if (el !== pad) return;
    const r = pad.getBoundingClientRect();
    const d = h('i', { class: 'ripple' + (right ? ' r' : ''), style: `left:${x - r.left}px;top:${y - r.top}px` });
    pad.append(d);
    setTimeout(() => d.remove(), 500);
  }

  el.addEventListener('pointerdown', e => {
    if (e.target.closest('button')) return;  // status pills etc.
    if (e.pointerType === 'mouse') return mouseDown(e);
    el.setPointerCapture(e.pointerId);
    stopInertia();
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY });
    const now = performance.now();
    if (pts.size === 1) {
      mode = 'pending'; moved = false; t0 = now; vel = 0; lastT = now;
      tapDrag = !isDirect() && now - lastTapAt < 300;
      clearTimeout(lpTimer);
      lpTimer = setTimeout(() => {
        if (mode !== 'pending' || moved) return;
        haptic(18);
        if (isDirect()) { moveTo(...toPc(e.clientX, e.clientY)); click('right'); mode = 'done'; }
        else { mode = 'drag'; press(); pad.classList.add('dragging'); }
      }, LONG);
      if (isDirect()) moveTo(...toPc(e.clientX, e.clientY));
    } else if (pts.size === 2 && (mode === 'pending' || mode === 'pinch-wait' || now - t0 < 200)) {
      clearTimeout(lpTimer);
      release();
      mode = 'two'; moved = false; pinch0 = { d: spread(), s: view.s, c: centroid(), tx: view.tx, ty: view.ty };
      scroll = { vx: 0, vy: 0, t: now };
    } else if (pts.size === 3) {
      release();
      mode = 'three'; moved = false;
    }
  });

  el.addEventListener('pointermove', e => {
    if (e.pointerType === 'mouse') return mouseMove(e);
    const p = pts.get(e.pointerId);
    if (!p) return;
    const now = performance.now();
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    if (pts.size === 1 && (mode === 'pending' || mode === 'move' || mode === 'drag' || mode === 'ddrag')) {
      p.x = e.clientX; p.y = e.clientY;
      if (!moved && Math.hypot(p.x - p.x0, p.y - p.y0) > SLOP) {
        moved = true;
        clearTimeout(lpTimer);
        if (mode === 'pending') {
          if (isDirect()) { mode = 'ddrag'; press(); }
          else if (tapDrag) { mode = 'drag'; press(); pad.classList.add('dragging'); haptic(10); }
          else mode = 'move';
        }
      }
      if (!moved) return;
      if (isDirect()) { moveTo(...toPc(e.clientX, e.clientY)); return; }
      const dt = Math.max(1, now - lastT);
      lastT = now;
      vel = 0.6 * vel + 0.4 * (Math.hypot(dx, dy) / dt);
      const g = gain(vel);
      moveBy(dx * g, dy * g);
      return;
    }
    p.x = e.clientX; p.y = e.clientY;
    if (mode === 'two' && pts.size === 2) {
      const [cx, cy] = centroid();
      const dist = spread();
      if (!moved && (Math.hypot(cx - pinch0.c[0], cy - pinch0.c[1]) > SLOP || Math.abs(dist - pinch0.d) > SLOP)) moved = true;
      if (!moved) return;
      // Decide once per gesture: pinch/pan the view, or scroll the PC.
      if (!pinch0.scrolling && !pinch0.zoom && direct && (Math.abs(dist / pinch0.d - 1) > 0.08 || view.s > 1.001))
        pinch0.zoom = true;
      const zooming = !!pinch0.zoom;
      if (!zooming) pinch0.scrolling = true;
      if (zooming) {
        pinch0.zoom = true;
        const r = stage.getBoundingClientRect();
        const s = clamp(pinch0.s * dist / pinch0.d, 1, 8);
        // keep the content point under the initial centroid under the fingers
        const px = (pinch0.c[0] - r.left - pinch0.tx) / pinch0.s, py = (pinch0.c[1] - r.top - pinch0.ty) / pinch0.s;
        setView(s, cx - r.left - px * s, cy - r.top - py * s);
      } else {
        const sdx = cx - (pinch0.lx ?? pinch0.c[0]), sdy = cy - (pinch0.ly ?? pinch0.c[1]);
        pinch0.lx = cx; pinch0.ly = cy;
        const k = 5 * settings.scroll * (settings.natural ? 1 : -1);
        wheel(sdy * k, -sdx * k);
        const dt = Math.max(1, now - scroll.t);
        scroll = { vx: 0.7 * scroll.vx + 0.3 * sdx / dt, vy: 0.7 * scroll.vy + 0.3 * sdy / dt, t: now };
      }
    } else if (mode === 'three') {
      if (!moved) {
        const [cx, cy] = centroid();
        const p0 = [...pts.values()].reduce((a, q) => [a[0] + q.x0 / 3, a[1] + q.y0 / 3], [0, 0]);
        const ddx = cx - p0[0], ddy = cy - p0[1];
        if (Math.hypot(ddx, ddy) > 45) {
          moved = true; haptic(12);
          const combo = Math.abs(ddy) > Math.abs(ddx) ? (ddy < 0 ? 'win+tab' : 'win+d') : 'alt+tab';
          input.send({ t: 'combo', s: combo });
          toast(combo === 'win+tab' ? 'Task view' : combo === 'win+d' ? 'Show desktop' : 'Switch app', { ic: 'hand', ms: 900 });
        }
      }
    }
  });

  const up = e => {
    if (e.pointerType === 'mouse') return mouseUp(e);
    const p = pts.get(e.pointerId);
    if (!p) return;
    pts.delete(e.pointerId);
    const now = performance.now();
    clearTimeout(lpTimer);
    if (mode === 'pending' && pts.size === 0) {
      if (now - t0 < TAP + 200 && e.type !== 'pointercancel') {
        if (isDirect()) click('left');
        else { click('left'); ripple(p.x, p.y); lastTapAt = now; }
        haptic(5);
      }
    } else if (mode === 'two') {
      if (pts.size === 0) {
        if (!moved && now - t0 < 400) { click('right'); ripple(p.x, p.y, true); haptic(8); }
        else if (!pinch0.zoom && Math.hypot(scroll.vx, scroll.vy) > 0.25 && now - scroll.t < 60) inertia(scroll.vx, scroll.vy);
      } else return;  // wait for the second finger
    } else if (mode === 'three') {
      if (pts.size) return;
      if (!moved) { click('middle'); haptic(8); }
    }
    if (pts.size === 0) { release(); mode = null; }
  };
  el.addEventListener('pointerup', up);
  el.addEventListener('pointercancel', up);

  // ---- momentum scrolling after a two-finger flick ----
  let inertiaRaf = 0;
  function inertia(vx, vy) {
    let last = performance.now();
    const k = 5 * settings.scroll * (settings.natural ? 1 : -1);
    const step = now => {
      const dt = now - last; last = now;
      const f = Math.pow(0.9955, dt);  // ~ iOS deceleration
      vx *= f; vy *= f;
      if (Math.hypot(vx, vy) < 0.02) { inertiaRaf = 0; return; }
      wheel(vy * dt * k, -vx * dt * k);
      inertiaRaf = requestAnimationFrame(step);
    };
    inertiaRaf = requestAnimationFrame(step);
  }
  function stopInertia() { if (inertiaRaf) cancelAnimationFrame(inertiaRaf); inertiaRaf = 0; }

  // ---- desktop mouse ----
  let mdown = null;
  function mouseDown(e) {
    el.focus({ preventScroll: true });
    if (direct) {
      moveTo(...toPc(e.clientX, e.clientY));
      button(['left', 'middle', 'right'][e.button] || 'left', true);
      el.setPointerCapture(e.pointerId);
    } else mdown = { x: e.clientX, y: e.clientY, b: e.button, moved: false };
  }
  function mouseMove(e) {
    if (direct) { if (document.activeElement === el || e.buttons) moveTo(...toPc(e.clientX, e.clientY)); return; }
    if (!mdown) return;
    const dx = e.clientX - mdown.x, dy = e.clientY - mdown.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) mdown.moved = true;
    mdown.x = e.clientX; mdown.y = e.clientY;
    const g = gain(0.4);
    moveBy(dx * g, dy * g);
  }
  function mouseUp(e) {
    if (direct) { button(['left', 'middle', 'right'][e.button] || 'left', false); return; }
    if (mdown && !mdown.moved) click(['left', 'middle', 'right'][mdown.b] || 'left');
    mdown = null;
  }
  el.addEventListener('contextmenu', e => e.preventDefault());
  el.addEventListener('wheel', e => {
    e.preventDefault();
    if (e.ctrlKey && direct) {  // trackpad pinch on desktop browsers
      const r = stage.getBoundingClientRect();
      const s = clamp(view.s * Math.exp(-e.deltaY / 200), 1, 8);
      const px = (e.clientX - r.left - view.tx) / view.s, py = (e.clientY - r.top - view.ty) / view.s;
      setView(s, e.clientX - r.left - px * s, e.clientY - r.top - py * s);
      return;
    }
    const unit = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? 400 : 1;
    wheel(-e.deltaY * unit * 1.2 * settings.scroll, e.deltaX * unit * 1.2 * settings.scroll);
  }, { passive: false });
}

// ================================================================ zoom ===
const view = { s: 1, tx: 0, ty: 0 };
let stageBox = { w: 0, h: 0 };
function setView(s, tx, ty) {
  const { w, h: hh } = stageBox;
  view.s = s;
  view.tx = clamp(tx, w - w * s, 0);
  view.ty = clamp(ty, hh - hh * s, 0);
  if (s <= 1.001) { view.s = 1; view.tx = 0; view.ty = 0; }
  inner.style.transform = `translate(${view.tx}px, ${view.ty}px) scale(${view.s})`;
  const zp = $('zoom-pill');
  zp.classList.toggle('hidden', view.s === 1);
  zp.textContent = view.s.toFixed(1) + '×';
  drawCursor();
  video.wantConfig();  // more pixels on screen -> ask for a sharper stream
}
function keepInView(sx, sy) {
  const m = 40, { w, h: hh } = stageBox;
  let tx = view.tx, ty = view.ty;
  if (sx < m) tx += m - sx; else if (sx > w - m) tx -= sx - (w - m);
  if (sy < m) ty += m - sy; else if (sy > hh - m) ty -= sy - (hh - m);
  if (tx !== view.tx || ty !== view.ty) {
    view.tx = clamp(tx, w - w * view.s, 0); view.ty = clamp(ty, hh - hh * view.s, 0);
    inner.style.transform = `translate(${view.tx}px, ${view.ty}px) scale(${view.s})`;
  }
}
$('zoom-pill').addEventListener('click', () => { haptic(6); setView(1, 0, 0); });

// ============================================================== layout ===
// Largest screen that fits while the pad / keys / input keep their room.
const MIN_PANEL = 220, MIN_PAD = 80;  // smallest sizes the grab bar drags the panel to
function layout() {
  const root = stage.parentElement;
  const d = disp(), ar = d.w / d.h, gap = 8;
  const typing = document.body.classList.contains('kb-open'), noPanel = !!settings.hidePanel && !typing;
  root.classList.toggle('no-panel', noPanel);
  // Before measuring: it changes the padding. Only while this tab is showing (resize callbacks fire after hide()).
  const theater = noPanel && root.closest('.view').classList.contains('active');
  document.body.classList.toggle('theater', theater);
  const cs = getComputedStyle(root);
  const W = root.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const H = root.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
  // Stacked (screen above the pad) vs side-by-side (controls on the right):
  // use whichever gives the bigger picture.
  const others = [strip, kb.closest('.inputbar'), $('clip-pill')].filter(x => !x.classList.contains('hidden'))
    .reduce((a, x) => a + (x.offsetHeight || 36) + gap, 0);
  // Beside or below the picture: decided by the screen's shape with the
  // automatic sizes, so dragging the panel wider can't flip the layout.
  const autoPad = typing ? 0 : Math.max(120, Math.min(220, H * 0.24)), autoPanel = clamp(W * 0.3, 260, 380);
  const side = !typing && H >= 200 && Math.min(W - autoPanel - gap, H * ar) > Math.min(W, (H - others - autoPad - gap) * ar) * 1.08;
  // ...then the sizes you dragged the grab bar to, keeping some picture.
  const minPad = typing || !settings.padH ? autoPad : clamp(settings.padH, MIN_PAD, Math.max(MIN_PAD, H - others - gap - 60));
  const panel = settings.panelW ? clamp(settings.panelW, MIN_PANEL, Math.max(MIN_PANEL, W - gap - 200)) : autoPanel;
  const stackedW = Math.max(0, Math.min(W, (H - others - minPad - gap) * ar));
  const sideW = Math.max(0, Math.min(W - panel - gap, H * ar));
  root.classList.toggle('side', side && !noPanel);
  root.style.setProperty('--panel-w', panel + 'px');
  document.body.classList.toggle('theater-b', theater && !side);
  let w = noPanel ? Math.min(W, H * ar) : side ? sideW : stackedW, hh = w / ar;
  if (typing && hh < 60) { w = 0; hh = 0; }
  w = Math.max(0, Math.floor(w)); hh = Math.max(0, Math.floor(hh));
  stage.style.width = w + 'px';
  stage.style.height = hh + 'px';
  stage.style.display = w ? '' : 'none';
  if (w !== stageBox.w || hh !== stageBox.h) {
    stageBox = { w, h: hh };
    setView(view.s, view.tx, view.ty);
  }
  placeGrip(root, side, noPanel, typing);
}
new ResizeObserver(() => layout()).observe(stage.parentElement);
// The panel's grab bar: in the gap between the picture and the panel, or at
// the screen edge while the panel is hidden.
function placeGrip(root, side, off, typing) {
  const g = $('panel-grip');
  g.classList.toggle('hidden', typing);
  if (typing) return;
  const rr = root.getBoundingClientRect(), cs = getComputedStyle(root);
  let x, y, dir;
  if (off) {
    dir = side ? 'left' : 'up';
    x = side ? rr.width - 9 : rr.width / 2;
    y = side ? rr.height / 2 : rr.height - 9;
  } else if (side) {
    dir = 'right';
    x = pad.getBoundingClientRect().left - rr.left - (parseFloat(cs.columnGap) || 8) / 2;
    y = rr.height / 2;
  } else {
    dir = 'down';
    x = rr.width / 2;
    y = stage.getBoundingClientRect().bottom - rr.top + (parseFloat(cs.rowGap) || 8) / 2;
  }
  g.className = `grip ${side ? 'v' : 'h'} ${dir}`;
  g.style.left = x + 'px';
  g.style.top = y + 'px';
  g.setAttribute('aria-label', g.title = off ? 'Show trackpad and keys' : 'Hide trackpad and keys');
}
// Hiding the panel also goes browser-fullscreen (where allowed, e.g. not on
// iPhone); leaving fullscreen (Esc) brings the panel back.
let fsByUs = false;
const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement;
function setPanelHidden(hide) {
  const de = document.documentElement, req = de.requestFullscreen || de.webkitRequestFullscreen;
  if (hide && !fsEl() && req) {
    fsByUs = true;
    try { Promise.resolve(req.call(de, { navigationUI: 'hide' })).catch(() => { fsByUs = false; }); } catch { fsByUs = false; }
  } else if (!hide && fsByUs && fsEl()) {
    fsByUs = false;
    (document.exitFullscreen || document.webkitExitFullscreen).call(document)?.catch?.(() => {});
  }
  setSetting('hidePanel', hide);
}
for (const ev of ['fullscreenchange', 'webkitfullscreenchange'])
  document.addEventListener(ev, () => {
    if (fsEl() || !fsByUs) return;
    fsByUs = false;
    if (settings.hidePanel) setSetting('hidePanel', false);
  });
// Tap the grab bar to hide / show the panel; drag it to resize the panel
// (its width beside the picture, the trackpad's height below it). Dragging
// on past where it stops shrinking hides it; dragging the edge bar out
// brings it back.
const grip = $('panel-grip'), PAST = 80;
let drag = null, dragged = false;
grip.addEventListener('pointerdown', e => {
  if (e.button > 0) return;
  const side = grip.classList.contains('v'), r = pad.getBoundingClientRect(), key = side ? 'panelW' : 'padH';
  drag = { id: e.pointerId, x: e.clientX, y: e.clientY, side, key, off: !!settings.hidePanel, moved: false,
           min: side ? MIN_PANEL : MIN_PAD, prev: settings[key], at: null,
           size0: settings.hidePanel ? 0 : side ? r.width : r.height };
  grip.setPointerCapture(e.pointerId);
});
grip.addEventListener('pointermove', e => {
  if (!drag || e.pointerId !== drag.id) return;
  const d = drag.side ? drag.x - e.clientX : drag.y - e.clientY;  // + = the panel grows
  if (!drag.moved && Math.abs(d) < 5) return;
  if (!drag.moved) { drag.moved = true; grip.classList.add('dragging'); }
  const size = drag.size0 + d;  // live below; saved on release
  if (!settings.hidePanel) {
    settings[drag.key] = Math.max(drag.min, Math.round(size));
    layout();
    const r = pad.getBoundingClientRect();
    if (size < (drag.side ? r.width : r.height) - PAST) {  // dragged well past its smallest: hide
      drag.at = size;
      settings[drag.key] = drag.prev;
      settings.hidePanel = true;
      haptic(8);
      layout();
    }
  } else if (size > (drag.at ?? PAST / 2) + 30) {  // out again from the edge (or back, mid-drag)
    settings.hidePanel = false;
    settings[drag.key] = Math.max(drag.min, Math.round(size));
    haptic(8);
    layout();
  }
});
const endDrag = e => {
  if (!drag || e.pointerId !== drag.id) return;
  const { moved, off, key } = drag;
  drag = null;
  grip.classList.remove('dragging');
  if (!moved) return;  // a tap: the click handler toggles
  dragged = true;
  setSetting(key, settings[key]);
  if (!!settings.hidePanel !== off) setPanelHidden(!!settings.hidePanel);
  else setSetting('hidePanel', !!settings.hidePanel);
};
grip.addEventListener('pointerup', endDrag);
grip.addEventListener('pointercancel', endDrag);
grip.addEventListener('click', () => {
  if (dragged) { dragged = false; return; }
  haptic(5);
  setPanelHidden(!settings.hidePanel);
});
bus.addEventListener('layout', () => active && layout());

// ====================================================== stream methods ===
const MS = window.ManagedMediaSource || window.MediaSource;
export const METHODS = [
  { id: 'auto', name: 'Automatic', sub: 'The best method this browser supports' },
  { id: 'h264', name: 'H.264', sub: 'Lowest latency · hardware decoded' },
  { id: 'hevc', name: 'HEVC', sub: 'Same latency · ~30% less data for the same picture' },
  { id: 'mse', name: 'Video player', sub: 'H.264 over plain HTTP · about 30–50 ms more delay' },
  { id: 'jpeg', name: 'JPEG', sub: 'Works anywhere · sends only what changed; full-screen video uses much more data' },
];
// '' = usable here, otherwise why not; null while still checking
export const support = { auto: '', h264: null, hevc: null, mse: null, jpeg: null };
export const supportReady = (async () => {
  const srv = INFO.video || {}, wc = !!window.VideoDecoder, sec = window.isSecureContext;
  support.h264 = !srv.h264?.length ? 'No H.264 encoder on the PC' : !sec ? 'Needs HTTPS' : !wc ? 'This browser can’t decode it' : '';
  let hevc = false;
  if (wc && sec) try { hevc = (await VideoDecoder.isConfigSupported({ codec: 'hvc1.1.6.L120.90', optimizeForLatency: true })).supported; } catch {}
  support.hevc = !srv.hevc?.length ? 'No HEVC encoder on the PC' : !sec ? 'Needs HTTPS' : !hevc ? 'This device can’t decode HEVC' : '';
  support.mse = !srv.h264?.length ? 'No H.264 encoder on the PC'
    : !(MS && MS.isTypeSupported('video/mp4; codecs="avc1.640028"')) ? 'Not supported by this browser' : '';
  support.jpeg = srv.mjpeg === false ? 'No JPEG encoder on the PC' : '';
  bus.dispatchEvent(new Event('support'));
})();
export function resolvePlan(want = settings.stream) {
  if (want !== 'auto' && support[want] === '') return { mode: want, alt: null };
  if (support.h264 === '') return { mode: 'h264', alt: null };
  // Plain HTTP: JPEG has the lowest latency on a decent link; the server
  // falls back to the H.264 player by itself if the link can't carry it.
  if (support.jpeg === '') return { mode: 'jpeg', alt: support.mse === '' ? 'mse' : null };
  return { mode: support.mse === '' ? 'mse' : 'jpeg', alt: null };
}
export const resolveMode = want => resolvePlan(want).mode;
// The quality <-> latency slider (mirrors video.tuning() on the server).
// Every level runs at the display's refresh rate; bitrate follows the link.
export const QUALITY = [
  { name: 'Fastest', sub: '3/4 resolution · smallest frames · lowest delay' },
  { name: 'Smooth', sub: 'Quick encode · small frames' },
  { name: 'Balanced', sub: 'Good detail' },
  { name: 'Sharp', sub: 'Full resolution · finer detail' },
  { name: 'Sharpest', sub: 'Full resolution · best detail · a little more delay' },
];
// Max bitrate steps (Mb/s); the last one is "no limit".
const MAXBR = [2, 3, 5, 8, 12, 20, 30, 50, 80, 0];
export const qualityLevel = q => QUALITY[Math.round(q * 4)];

// =============================================================== video ===
const HDR = 16, KIND_FRAME = 2, KIND_JPEG = 3, KIND_FMP4 = 4, KIND_TILES = 5;
const supported = new Set();  // codec strings the decoder accepted
const vid = $('screen-video');
function surface(kind) {
  canvas.hidden = kind !== 'canvas';
  vid.hidden = kind !== 'video';
}
// <video> + Media Source Extensions: fragmented MP4, one fragment per frame.
// The stream has a single keyframe, so seeking would re-decode from the
// start; instead playback runs slightly fast whenever it lags the live edge.
const mse = {
  ms: null, sb: null, q: [], mime: '', url: '', marks: [],
  config(m) {
    const mime = `video/mp4; codecs="${m.codec}"`;
    const init = Uint8Array.from(atob(m.init), c => c.charCodeAt(0));
    surface('video');
    if (this.ms) {
      if (mime !== this.mime) this.q.push({ type: mime });
      this.mime = mime;
      this.q.push(init);
      return this.pump();
    }
    this.mime = mime;
    this.q = [init];
    this.ms = new MS();
    if (window.ManagedMediaSource && this.ms instanceof window.ManagedMediaSource) vid.disableRemotePlayback = true;
    this.url = URL.createObjectURL(this.ms);
    vid.src = this.url;
    this.ms.addEventListener('sourceopen', () => {
      try { this.ms.duration = Infinity; } catch {}  // live: no end, low-delay rendering
      this.sb = this.ms.addSourceBuffer(this.mime);
      this.sb.mode = 'sequence';  // each new encoder's timestamps continue the timeline
      this.sb.addEventListener('updateend', () => this.onAppended());
      this.pump();
    }, { once: true });
  },
  append(data, t) { this.q.push(data); this.q.at(-1).t = t; this.pump(); },
  pump() {
    const sb = this.sb;
    if (!sb || sb.updating) return;
    while (this.q.length && this.q[0].type) { const { type } = this.q.shift(); try { sb.changeType(type); } catch {} }
    const b = vid.buffered;
    if (b.length && vid.currentTime - b.start(0) > 12) { try { sb.remove(0, vid.currentTime - 4); } catch {} return; }
    if (!this.q.length) return;
    const next = this.q.shift();
    this.pending = next.t;
    try { sb.appendBuffer(next); }
    catch (e) { console.warn('mse', e); }
  },
  onAppended() {
    const b = vid.buffered;
    if (b.length) {
      const end = b.end(b.length - 1);
      if (this.pending) { this.marks.push([end, this.pending]); if (this.marks.length > 240) this.marks.shift(); this.pending = 0; }
      if (vid.paused) vid.play().catch(() => {});
      const lag = end - vid.currentTime, budget = Math.max(0.025, 1.5 / (video.cfg?.fps || 60));
      vid.playbackRate = lag > budget ? Math.min(2, 1 + (lag - budget) * 8) : 1;  // chase the live edge
      video.st.buf = lag * 1000;
    }
    this.pump();
  },
  reset() {
    if (this.url) URL.revokeObjectURL(this.url);
    vid.removeAttribute('src');
    try { vid.load(); } catch {}
    Object.assign(this, { ms: null, sb: null, q: [], url: '', marks: [] });
  },
};
// Presented-frame timing: which appended fragment is on screen now.
if (vid.requestVideoFrameCallback) {
  const onFrame = (now, meta) => {
    const mark = mse.marks.find(([end]) => end >= meta.mediaTime - 1e-3);
    if (mark && video.offset) video.st.lat = 0.9 * video.st.lat + 0.1 * (meta.expectedDisplayTime + video.offset - mark[1]);
    video.st.frames++;
    video.lastDraw = performance.now();
    vid.requestVideoFrameCallback(onFrame);
  };
  vid.requestVideoFrameCallback(onFrame);
}

const video = {
  ws: null, mode: null, dec: null, cfg: null, gen: -1, retry: 0, ctx: null, want: null,
  sentCfg: '', cfgTimer: 0, offset: 0, rtt: 0, needKey: true, jpegBusy: false, jpegNext: null, tileQ: [], tileBusy: false,
  st: { frames: 0, bytes: 0, t: performance.now(), fps: 0, mbps: 0, lat: 0, dec: 0, buf: 0, server: null },
  lastDraw: 0, times: new Map(), held: null,

  start() {
    if (this.ws) return;
    if (!INFO.video?.ffmpeg) { message('Screen streaming needs <b>ffmpeg</b> on the PC.<br><code>winget install Gyan.FFmpeg</code> then restart the server.'); return; }
    this.plan = resolvePlan();
    this.mode = this.plan.mode;  // what is actually streaming (the server may use plan.alt)
    this.ctx = this.ctx || canvas.getContext('2d', { alpha: false, desynchronized: true });
    status('warn', 'Connecting');
    const ws = this.ws = new WebSocket(wsUrl('/vstream'));
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { this.retry = 0; this.sentCfg = ''; this.sendCfg(); this.ping(); };
    ws.onmessage = e => typeof e.data === 'string' ? this.onText(JSON.parse(e.data)) : this.onFrame(e.data);
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.closeDecoder();
      status('bad', 'Offline');
      if (active && !document.hidden) setTimeout(() => this.start(), Math.min(3000, 250 * 2 ** this.retry++));
    };
    clearInterval(this.pingT);
    this.pingT = setInterval(() => this.ping(), 2000);
  },
  stop() {
    clearInterval(this.pingT);
    const ws = this.ws;
    this.ws = null;
    if (ws) ws.close();
    this.closeDecoder();
  },
  send(m) { if (this.ws?.readyState === 1) this.ws.send(JSON.stringify(m)); },
  ping() { this.send({ t: 'ping', c: performance.now() }); },
  desired() {
    const dpr = window.devicePixelRatio || 1;
    return { t: 'cfg', mode: this.plan.mode, alt: this.plan.alt, display: dispIdx, q: settings.quality,
      maxbr: settings.maxMbps ? settings.maxMbps * 1e6 : 0,
      w: Math.round(stageBox.w * dpr * view.s), h: Math.round(stageBox.h * dpr * view.s), fps: refreshRate };
  },
  wantConfig() {
    clearTimeout(this.cfgTimer);
    this.cfgTimer = setTimeout(() => this.sendCfg(), 350);
  },
  sendCfg() {
    if (!stageBox.w) return;
    const c = this.desired();
    // Resolution changes under ~10% aren't worth an encoder switch.
    const prev = this.sentCfg && JSON.parse(this.sentCfg);
    if (prev && prev.mode === c.mode && prev.alt === c.alt && prev.display === c.display && prev.fps === c.fps
        && prev.q === c.q && prev.maxbr === c.maxbr && Math.abs(c.w / prev.w - 1) < 0.1) return;
    this.sentCfg = JSON.stringify(c);
    this.send(c);
  },

  onText(m) {
    if (m.t === 'pong') {
      const now = performance.now(), rtt = now - m.c;
      if (!this.rtt || rtt < this.rtt * 1.5) this.offset = m.s - (m.c + rtt / 2);
      this.lastPong = now;
      this.rtt = this.rtt ? 0.8 * this.rtt + 0.2 * rtt : rtt;
    } else if (m.t === 'sping') {
      this.send({ t: 'spong', s: m.s });  // lets the server measure pure propagation delay
    } else if (m.t === 'config') {
      if (this.mode !== m.mode && /^(slow link: switched|link improved)/.test(m.reason || ''))
        toast(m.mode === 'jpeg' ? 'Connection improved — back to JPEG' : 'Slow connection — switched to H.264',
              { ic: 'pulse', ms: 2200 });
      this.mode = m.mode;
      if (m.mode === 'jpeg') { this.cfg = m; this.gen = m.gen; this.tileQ = []; mse.reset(); surface('canvas'); message(null); }
      else this.configure(m);
    } else if (m.t === 'stats') {
      this.st.server = m;
    } else if (m.t === 'error') {
      if (!m.retry) message(m.msg);
      else status('bad', 'Restarting');
    }
  },
  async configure(m) {
    this.cfg = m;
    this.gen = m.gen;
    if (m.mode === 'mse') { mse.config(m); message(null); return; }
    surface('canvas');
    const config = { codec: m.codec, description: Uint8Array.from(atob(m.desc), c => c.charCodeAt(0)),
                     optimizeForLatency: true, hardwareAcceleration: 'no-preference' };
    if (!supported.has(m.codec)) {
      // Frames that arrive during this async check are held, not dropped:
      // losing the stream's only keyframe would stall it for good. A newer
      // config arriving meanwhile takes over the same buffer.
      this.held = this.held || [];
      this.checking = m;
      let ok = false;
      try { ok = (await VideoDecoder.isConfigSupported(config)).supported; } catch {}
      if (this.checking !== m) return;  // superseded; the newer config drains `held`
      this.checking = null;
      if (!ok) {
        this.held = null;
        support[m.mode] = 'This device’s decoder refused it';
        this.plan = resolvePlan();
        this.mode = this.plan.mode;
        toast(`${m.codec.startsWith('hvc') ? 'HEVC' : 'H.264'} isn’t decodable here — switching to ${METHODS.find(x => x.id === this.mode).name}`, { err: true });
        this.sentCfg = '';
        this.sendCfg();
        return;
      }
      supported.add(m.codec);
    }
    this.checking = null;
    this.setupDecoder(config);
    const held = this.held || [];
    this.held = null;
    for (const b of held) this.onFrame(b, true);  // older generations are skipped by gen
  },
  setupDecoder(config) {
    if (!this.dec || this.dec.state === 'closed') {
      this.dec = new VideoDecoder({
        output: f => this.draw(f),
        error: e => { console.warn('decoder', e); this.needKey = true; this.dec = null; this.send({ t: 'kf' }); },
      });
    }
    this.dec.configure(config);
    this.needKey = true;
    this.needKeySince = performance.now();
    message(null);
  },
  closeDecoder() {
    try { this.dec && this.dec.state !== 'closed' && this.dec.close(); } catch {}
    this.dec = null;
    this.gen = -1;
    this.held = null;
    this.checking = null;
    mse.reset();
  },
  onFrame(buf, replay = false) {
    const dv = new DataView(buf);
    const kind = dv.getUint8(0), key = dv.getUint8(1) & 1, gen = dv.getUint16(2, true), seq = dv.getUint32(4, true);
    const t = dv.getFloat64(8, true);
    // ACK on arrival (even if held for decode) so rate control sees the link, not our config check.
    if (!replay) { this.send({ t: 'ack', s: seq }); this.st.bytes += buf.byteLength; }
    if (kind === 0) return;  // capacity probe: the ACK was the point
    if (this.held && !replay) { this.held.push(buf); return; }
    if (kind === KIND_FMP4) {
      if (gen === this.gen) mse.append(new Uint8Array(buf, HDR), t);
      return;
    }
    if (kind === KIND_TILES) {
      if (gen !== this.gen) return;
      this.tileQ.push({ buf, t, gen, full: key });
      if (!this.tileBusy) this.drawTiles();
      return;
    }
    if (kind === KIND_JPEG) {
      this.jpegNext = { data: new Blob([new Uint8Array(buf, HDR)], { type: 'image/jpeg' }), t };
      if (!this.jpegBusy) this.decodeJpeg();
      return;
    }
    if (kind !== KIND_FRAME || !this.dec || this.dec.state !== 'configured' || gen !== this.gen) return;
    if (this.needKey && !key) {
      const now = performance.now();
      if (now - this.needKeySince > 400 && now - (this.kfAsked || 0) > 1000) { this.kfAsked = now; this.send({ t: 'kf' }); }
      return;
    }
    this.needKey = false;
    this.times.set(seq, [t, performance.now()]);
    if (this.times.size > 240) this.times.delete(this.times.keys().next().value);
    try {
      this.dec.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: seq * 1000, data: new Uint8Array(buf, HDR) }));
    } catch (e) {
      this.needKey = true;
      this.send({ t: 'kf' });
    }
  },
  async decodeJpeg() {
    this.jpegBusy = true;
    while (this.jpegNext) {
      const { data, t } = this.jpegNext;
      this.jpegNext = null;  // newest wins: frames that arrive mid-decode are skipped
      try {
        const t1 = performance.now();
        const bmp = await createImageBitmap(data);
        this.times.set(-1, [t, t1]);
        this.draw(bmp, -1);
      } catch {}
    }
    this.jpegBusy = false;
  },
  // Tiled JPEG: the changed parts of the screen, painted over what's there.
  // Unlike whole JPEGs none can be skipped, except by a full-screen one.
  async drawTiles() {
    this.tileBusy = true;
    while (this.tileQ.length) {
      let i = this.tileQ.length - 1;
      while (i > 0 && !this.tileQ[i].full) i--;
      const { buf, t, gen } = this.tileQ[i];
      this.tileQ.splice(0, i + 1);
      const dv = new DataView(buf, HDR), n = dv.getUint16(0, true), rects = [];
      let off = HDR + 2 + n * 12;
      for (let k = 0; k < n; k++) {
        const o = 2 + k * 12, len = dv.getUint32(o + 8, true);
        rects.push({ x: dv.getUint16(o, true), y: dv.getUint16(o + 2, true),
                     blob: new Blob([new Uint8Array(buf, off, len)], { type: 'image/jpeg' }) });
        off += len;
      }
      const t1 = performance.now();
      let bmps;
      try { bmps = await Promise.all(rects.map(r => createImageBitmap(r.blob))); }
      catch { this.tileQ = []; this.send({ t: 'kf' }); continue; }  // a broken image: resend the whole screen
      if (gen !== this.gen) { bmps.forEach(b => b.close()); continue; }
      if (canvas.hidden) surface('canvas');
      const c = this.cfg;
      if (c && (canvas.width !== c.w || canvas.height !== c.h)) { canvas.width = c.w; canvas.height = c.h; }
      rects.forEach((r, k) => { this.ctx.drawImage(bmps[k], r.x, r.y); bmps[k].close(); });
      const now = performance.now();
      this.st.dec = 0.9 * this.st.dec + 0.1 * (now - t1);
      if (this.offset) this.st.lat = 0.9 * this.st.lat + 0.1 * (now + this.offset - t);
      this.st.frames++;
      this.lastDraw = now;
      if (stageMsgShown) message(null);
    }
    this.tileBusy = false;
  },
  draw(frame, seq) {
    if (canvas.hidden) surface('canvas');
    const w = frame.displayWidth || frame.width, hh = frame.displayHeight || frame.height;
    if (canvas.width !== w || canvas.height !== hh) { canvas.width = w; canvas.height = hh; }
    this.ctx.drawImage(frame, 0, 0, w, hh);
    if (seq === undefined) seq = frame.timestamp / 1000;
    frame.close();
    const now = performance.now();
    const tt = this.times.get(seq);
    if (tt) {
      this.times.delete(seq);
      this.st.dec = 0.9 * this.st.dec + 0.1 * (now - tt[1]);
      // server read-out time -> drawn, in the client's clock
      if (this.offset) this.st.lat = 0.9 * this.st.lat + 0.1 * (now + this.offset - tt[0]);
    }
    this.st.frames++;
    this.lastDraw = now;
    if (stageMsgShown) message(null);
  },
};

// frame / bitrate counters + HUD
setInterval(() => {
  const s = video.st, now = performance.now(), dt = (now - s.t) / 1000;
  s.fps = s.frames / dt; s.mbps = s.bytes * 8 / dt / 1e6; s.frames = 0; s.bytes = 0; s.t = now;
  if (!video.ws) return;
  const sv = s.server || {};
  const tag = { h264: '', hevc: 'HEVC · ', mse: 'Player · ', jpeg: 'JPEG · ' }[video.mode] ?? '';
  // JPEG sends nothing while the screen is still; that's healthy, not stalled.
  const still = video.mode === 'jpeg' && s.fps < 1 && video.ws.readyState === 1 && now - (video.lastPong || 0) < 5000;
  status(video.ws.readyState === 1 && (now - video.lastDraw < 3000 || still) ? 'ok' : 'warn',
         still ? `${tag}still` : `${tag}${Math.round(s.fps)} fps`);
  const hud = $('hud');
  hud.classList.toggle('hidden', !settings.stats);
  if (settings.stats) {
    // Server figures only when they describe what's actually streaming; unknown ones are left out, not shown as '?'.
    const srv = sv.mode === video.mode ? sv : {};
    const enc = video.mode === 'jpeg' ? (srv.enc === 'tiles' ? 'JPEG tiles' : 'MJPEG') : `${video.mode === 'hevc' ? 'HEVC' : 'H.264'} ${srv.enc || ''}`;
    const tune = [video.mode === 'jpeg' ? srv.jq != null && `q:v ${srv.jq}` : srv.cq != null && `cq ${srv.cq} ${srv.preset}`,
                  srv.qd != null && `queue ${srv.qd} ms`].filter(Boolean).join('  ');
    hud.textContent = [
      `${enc}${video.mode === 'mse' ? ' → <video>' : ''}${srv.w ? `  ${srv.w}×${srv.h}` : ''}${srv.native ? ' native' : ''}`,
      `${s.fps.toFixed(0)}${srv.fps ? '/' + srv.fps : ''} fps  ${s.mbps.toFixed(1)} Mb/s${srv.cap ? ' / ' + (srv.cap / 1e6).toFixed(0) : ''}`,
      `latency ≈ ${Math.max(0, s.lat).toFixed(1)} ms  rtt ${video.rtt.toFixed(1)}  ` +
        (video.mode === 'mse' ? `buffer ${s.buf.toFixed(0)}` : `dec ${s.dec.toFixed(1)}`),
      tune && `${qualityLevel(settings.quality).name}  ${tune}`,
    ].filter(Boolean).join('\n');
  }
}, 1000);

function status(kind, text) {
  const p = $('status-pill');
  p.querySelector('.dot').className = 'dot ' + kind;
  p.querySelector('span').textContent = text;
}
$('status-pill').addEventListener('click', () => { haptic(5); setSetting('stats', !settings.stats); });
let stageMsgShown = false;
function message(html) {
  const m = $('stage-msg');
  stageMsgShown = !!html;
  m.classList.toggle('hidden', !html);
  if (html) m.innerHTML = html;
}

// Measure the device's display refresh so we can ask for a matching frame
// rate (60 / 90 / 120 / 144 ...).
let refreshRate = 60;
(function measure() {
  let n = 0, t0 = 0;
  const f = t => {
    if (!t0) t0 = t;
    if (++n < 40) return requestAnimationFrame(f);
    const hz = 1000 * (n - 1) / (t - t0);
    refreshRate = [60, 75, 90, 120, 144, 165, 240].reduce((a, b) => Math.abs(b - hz) < Math.abs(a - hz) ? b : a);
    if (video.ws) video.wantConfig();
  };
  requestAnimationFrame(f);
})();

// =========================================================== keyboard ===
const MODS = ['ctrl', 'alt', 'shift', 'win'];
const MOD_CODE = { ctrl: 'ControlLeft', alt: 'AltLeft', shift: 'ShiftLeft', win: 'MetaLeft' };
const MOD_LABEL = { ctrl: 'Ctrl', alt: 'Alt', shift: '⇧', win: '⊞' };
const modState = { ctrl: 0, alt: 0, shift: 0, win: 0 };  // 0 off, 1 once, 2 locked
const activeMods = () => MODS.filter(m => modState[m]);
function consumeMods() {
  let changed = false;
  for (const m of MODS) if (modState[m] === 1) { modState[m] = 0; changed = true; }
  if (changed) renderMods();
}
function renderMods() {
  for (const m of MODS) {
    const c = strip.querySelector(`[data-mod="${m}"]`);
    if (c) { c.classList.toggle('on', modState[m] === 1); c.classList.toggle('lock', modState[m] === 2); }
  }
}
function sendKey(name) {
  const m = activeMods();
  input.send({ t: 'combo', s: [...m, name].join('+') });
  consumeMods();
}
const KEYS = [
  ['Esc', 'esc'], ['Tab', 'tab'], ['←', 'left'], ['↑', 'up'], ['↓', 'down'], ['→', 'right'], ['⌫', 'backspace'],
  ['Del', 'delete'], ['Home', 'home'], ['End', 'end'], ['PgUp', 'pageup'], ['PgDn', 'pagedown'],
  ...Array.from({ length: 12 }, (_, i) => ['F' + (i + 1), 'f' + (i + 1)]), ['PrtSc', 'printscreen'], ['Ins', 'insert'],
];
function renderStrip() {
  const kids = [];
  for (const m of MODS) {
    let tapT = 0;
    kids.push(h('button', { class: 'chip mod', 'data-mod': m, onclick: () => {
      const now = performance.now();
      // tap: one-shot, double-tap: lock, tap again: off
      modState[m] = modState[m] ? (now - tapT < 350 && modState[m] === 1 ? 2 : 0) : 1;
      tapT = now;
      haptic(6);
      renderMods();
    } }, MOD_LABEL[m]));
  }
  kids.push(h('span', { class: 'chip sep' }));
  for (const c of settings.recents.slice(0, 5)) kids.push(h('button', { class: 'chip recent', onclick: () => { haptic(6); sendCombo(c); } }, prettyCombo(c)));
  if (settings.recents.length) kids.push(h('span', { class: 'chip sep' }));
  for (const [label, name] of KEYS) kids.push(h('button', { class: 'chip', onclick: () => { haptic(5); sendKey(name); } }, label));
  strip.replaceChildren(...kids);
  renderMods();
}
const prettyCombo = c => c.split('+').map(p => ({ ctrl: 'Ctrl', alt: 'Alt', shift: '⇧', win: '⊞', esc: 'Esc',
  tab: 'Tab', enter: '⏎', delete: 'Del', backspace: '⌫', left: '←', right: '→', up: '↑', down: '↓' })[p] ||
  (p.length === 1 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1))).join('+');
function sendCombo(c) {
  input.send({ t: 'combo', s: c });
  const r = [c, ...settings.recents.filter(x => x !== c)].slice(0, 8);
  setSetting('recents', r);
  renderStrip();
}

// ---- live typing field: every keystroke goes to the PC immediately ----
// The field keeps a zero-width sentinel so Backspace always has something
// to delete (soft keyboards send nothing on an empty field). Changes are
// applied as a diff (backspaces + inserted text), which also handles
// autocorrect, swipe typing, dictation and paste.
const SENT = String.fromCharCode(0x200B), SENT_RE = new RegExp(SENT, 'g');
let prevVal = SENT;
kb.value = SENT;
function resetField() { kb.value = SENT; prevVal = SENT; }
kb.addEventListener('focus', () => { kb.closest('.field').classList.add('live'); if (!kb.value) resetField(); });
kb.addEventListener('blur', () => { kb.closest('.field').classList.remove('live'); resetField(); });
kb.addEventListener('input', () => {
  const raw = kb.value, a = prevVal;
  let p = 0;
  while (p < a.length && p < raw.length && a[p] === raw[p]) p++;
  let del = a.length - p;
  // Deleting the sentinel on an "empty" field is one real Backspace; if it
  // went along with other characters (select-all + delete), it isn't.
  if (p === 0 && del > 1 && a.startsWith(SENT)) del--;
  const ins = raw.slice(p).replace(SENT_RE, '');
  const v = SENT + raw.replace(SENT_RE, '');
  const mods = activeMods();
  if (mods.length && ins.length === 1 && !del) {
    input.send({ t: 'combo', s: [...mods, ins.toLowerCase() === ' ' ? 'space' : ins.toLowerCase()].join('+') });
    consumeMods();
    kb.value = prevVal;  // a shortcut, not text
    return;
  }
  if (del) input.send({ t: 'bs', n: del });
  if (ins) input.send({ t: 'text', s: ins });
  if (v.length <= 1 || v.length > 120) resetField();
  else { if (kb.value !== v) kb.value = v; prevVal = v; }
});
kb.addEventListener('keydown', e => {
  if (e.isComposing || e.keyCode === 229) return;
  const special = { Enter: 'enter', Tab: 'tab', Escape: 'esc', ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up',
    ArrowDown: 'down', Home: 'home', End: 'end', PageUp: 'pageup', PageDown: 'pagedown', Delete: 'delete' };
  if (special[e.key] || /^F\d\d?$/.test(e.key)) {
    e.preventDefault();
    sendKey(special[e.key] || e.key.toLowerCase());
    if (e.key === 'Enter') resetField();
  } else if (e.key === 'Backspace' && (kb.value === SENT || kb.value === '')) {
    e.preventDefault();
    input.send({ t: 'bs', n: 1 });
  } else if ((e.ctrlKey || e.metaKey || e.altKey) && e.key.length === 1) {
    e.preventDefault();
    const parts = [e.ctrlKey && 'ctrl', e.altKey && 'alt', e.shiftKey && 'shift', e.metaKey && 'win'].filter(Boolean);
    input.send({ t: 'combo', s: [...parts, e.code.startsWith('Key') ? e.code.slice(3).toLowerCase() : e.key.toLowerCase()].join('+') });
  }
});

// ---- raw keyboard capture when the screen or pad has focus (desktop) ----
const held = new Set();
function rawKey(e, down) {
  if (e.isComposing) return;
  e.preventDefault();
  if (down) { if (held.has(e.code) && !e.repeat) return; held.add(e.code); } else held.delete(e.code);
  input.send({ t: 'key', code: e.code, d: down ? 1 : 0 });
}
for (const el of [stage, pad]) {
  el.addEventListener('keydown', e => rawKey(e, true));
  el.addEventListener('keyup', e => rawKey(e, false));
  el.addEventListener('blur', () => { if (held.size) { input.send({ t: 'rel' }); held.clear(); } });
}

// ============================================================ shortcuts ===
const SHORTCUTS = [
  ['Copy', 'ctrl+c'], ['Paste', 'ctrl+v'], ['Cut', 'ctrl+x'], ['Undo', 'ctrl+z'], ['Redo', 'ctrl+y'],
  ['Select all', 'ctrl+a'], ['Save', 'ctrl+s'], ['Find', 'ctrl+f'], ['Switch app', 'alt+tab'],
  ['Close window', 'alt+f4'], ['Show desktop', 'win+d'], ['Task view', 'win+tab'], ['File Explorer', 'win+e'],
  ['Run', 'win+r'], ['Settings', 'win+i'], ['Snip screenshot', 'win+shift+s'], ['Clipboard history', 'win+v'],
  ['Emoji panel', 'win+.'], ['Task Manager', 'ctrl+shift+esc'], ['New tab', 'ctrl+t'], ['Close tab', 'ctrl+w'],
  ['Reopen closed tab', 'ctrl+shift+t'], ['Next tab', 'ctrl+tab'], ['Previous tab', 'ctrl+shift+tab'],
  ['Refresh', 'f5'], ['Full screen', 'f11'], ['Zoom in', 'ctrl+plus'], ['Zoom out', 'ctrl+minus'],
  ['Reset zoom', 'ctrl+0'], ['Rename', 'f2'], ['Snap left', 'win+left'], ['Snap right', 'win+right'],
  ['Maximize', 'win+up'], ['Minimize', 'win+down'], ['Minimize all', 'win+m'], ['Next desktop', 'ctrl+win+right'],
  ['Previous desktop', 'ctrl+win+left'], ['Game Bar', 'win+g'], ['Volume up', 'volumeup'], ['Volume down', 'volumedown'],
  ['Play / pause', 'playpause'],
];
function shortcutSheet() {
  const q = h('input', { class: 'input', placeholder: 'Search or type a combo, e.g. ctrl+shift+t', autocomplete: 'off',
    autocapitalize: 'off', spellcheck: 'false', enterkeyhint: 'send' });
  const list = h('div', { class: 'card' });
  const s = sheet({ title: 'Shortcuts', body: h('div', { class: 'form' }, q, list) });
  const fire = c => { haptic(8); sendCombo(c); s.close(); toast(prettyCombo(c), { ic: 'command', ms: 900 }); };
  const render = () => {
    const t = q.value.trim().toLowerCase();
    const items = [...settings.recents.map(c => [SHORTCUTS.find(x => x[1] === c)?.[0] || 'Recent', c, true]),
                   ...SHORTCUTS.filter(x => !settings.recents.includes(x[1])).map(x => [...x, false])]
      .filter(([n, c]) => !t || n.toLowerCase().includes(t) || c.includes(t.replace(/\s/g, '')));
    const rows = items.slice(0, 60).map(([n, c, r]) => h('button', { class: 'list-row', onclick: () => fire(c) },
      r ? ico('restart', 'ic') : null, h('span', { class: 'nm' }, n), h('span', { class: 'k' }, prettyCombo(c))));
    if (/^[a-z0-9]+(\+[a-z0-9.,/;'`[\]\\=-]+)+$|^f\d\d?$/.test(t) && !items.some(x => x[1] === t))
      rows.unshift(h('button', { class: 'list-row', onclick: () => fire(t) }, ico('command', 'ic'),
        h('span', { class: 'nm' }, 'Send'), h('span', { class: 'k' }, prettyCombo(t))));
    list.replaceChildren(...(rows.length ? rows : [h('div', { class: 'empty' }, 'No match')]));
  };
  q.addEventListener('input', render);
  q.addEventListener('keydown', e => { if (e.key === 'Enter') { const b = list.querySelector('.list-row'); if (b) b.click(); } });
  render();
  if (matchMedia('(pointer: fine)').matches) setTimeout(() => q.focus(), 250);
}
$('btn-shortcut').addEventListener('click', () => { haptic(5); shortcutSheet(); });

// ============================================================ clipboard ===
let clipText = '', clipT = 0;
function showClip(text) {
  clipText = text;
  const p = $('clip-pill');
  $('clip-text').textContent = 'PC copied: ' + text.replace(/\s+/g, ' ').slice(0, 120);
  p.classList.remove('hidden');
  clearTimeout(clipT);
  clipT = setTimeout(() => { p.classList.add('hidden'); layout(); }, 9000);
  layout();
}
$('clip-copy').addEventListener('click', async () => {
  const ok = await copyToDevice(clipText);
  toast(ok ? `Copied ${clipText.length} characters` : 'Copy failed', { err: !ok, ic: ok ? 'check' : null });
  $('clip-pill').classList.add('hidden');
  layout();
});
$('clip-x').addEventListener('click', () => { $('clip-pill').classList.add('hidden'); layout(); });

// ============================================================= settings ===
// Building blocks shared by the Remote settings sheet and the Controls tab.
function slRow(label, key, min, max, step, fmt) {
  const v = h('b', {}, fmt(settings[key]));
  const s = new Slider({ value: settings[key], min, max, step, thin: true, format: () => '',
    onInput: x => { v.textContent = fmt(x); setSetting(key, x); } });
  return h('div', { class: 'setting col' }, h('div', { class: 'hd' }, h('span', {}, label), v), s.el);
}
function swRow(label, key, desc) {
  return h('div', { class: 'setting' },
    h('div', { class: 't' }, h('div', {}, label), desc ? h('div', { class: 'd' }, desc) : null),
    toggle(settings[key], v => setSetting(key, v)));
}
function segRow(label, key, opts) {
  return h('div', { class: 'setting' }, h('div', { class: 't' }, label),
    h('div', { class: 'seg' }, opts.map(([v, l]) => {
      const b = h('button', { class: settings[key] === v ? 'on' : '', onclick: () => {
        setSetting(key, v); b.parentElement.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
        haptic(5);
      } }, l);
      return b;
    })));
}

/** Stream method picker + quality/latency controls. */
export function streamSettings() {
  const list = h('div', { class: 'card' });
  const drawMethods = () => {
    list.replaceChildren(...METHODS.map(m => {
      const why = support[m.id];
      const on = settings.stream === m.id;
      const p = resolvePlan('auto');
      const auto = m.id === 'auto' ? METHODS.find(x => x.id === p.mode).name + (p.alt ? ', H.264 on slow links' : '') : '';
      return h('button', { class: 'list-row method' + (on ? ' on' : '') + (why ? ' off' : ''),
        onclick: () => {
          if (why) { toast(why); return; }
          haptic(6);
          setSetting('stream', m.id);
          drawMethods();
        } },
        h('div', { class: 'mt' }, h('div', {}, m.name, auto ? h('span', { class: 'hint' }, ` · ${auto}`) : null),
          h('div', { class: 'd' }, why === null ? 'Checking…' : why || m.sub)),
        h('span', { class: 'check', icon: 'check' }));
    }));
  };
  drawMethods();
  bus.addEventListener('support', drawMethods);
  const lvl = qualityLevel(settings.quality);
  const name = h('b', {}, lvl.name), sub = h('div', { class: 'd' }, lvl.sub);
  const q = new Slider({ value: settings.quality, min: 0, max: 1, step: 0.25, thin: true, format: () => '',
    onInput: v => { const l = qualityLevel(v); name.textContent = l.name; sub.textContent = l.sub; setSetting('quality', v); } });
  const brLabel = i => MAXBR[i] ? `${MAXBR[i]} Mb/s` : 'No limit';
  const brIdx = Math.max(0, MAXBR.indexOf(settings.maxMbps || 0));
  const brName = h('b', {}, brLabel(brIdx));
  const br = new Slider({ value: brIdx, min: 0, max: MAXBR.length - 1, step: 1, thin: true, format: () => '',
    onInput: i => { i = Math.round(i); brName.textContent = brLabel(i); setSetting('maxMbps', MAXBR[i]); } });
  return h('div', {},
    h('div', { class: 'group-title' }, 'Stream method'), list,
    h('div', { class: 'group-title' }, 'Picture'),
    h('div', { class: 'card' },
      h('div', { class: 'setting col' },
        h('div', { class: 'hd' }, h('span', {}, 'Speed ↔ Quality'), name), q.el,
        h('div', { class: 'ends' }, h('span', {}, 'Lower latency'), h('span', {}, 'Sharper')), sub),
      h('div', { class: 'setting col' },
        h('div', { class: 'hd' }, h('span', {}, 'Max bitrate'), brName), br.el,
        h('div', { class: 'd' }, 'Caps the stream’s data use, e.g. on mobile data. It always runs at full frame rate and uses as much of the link as it can, up to this.')),
      swRow('Show stats', 'stats', 'Tap the status pill to toggle')));
}

function settingsSheet() {
  const x = v => v.toFixed(1) + '×';
  sheet({ title: 'Settings', body: h('div', {},
    h('div', { class: 'group-title' }, 'Trackpad'),
    h('div', { class: 'card' },
      slRow('Pointer speed', 'speed', 0.3, 3, 0.05, x),
      slRow('Acceleration', 'accel', 0, 1, 0.05, v => Math.round(v * 100) + '%'),
      slRow('Scroll speed', 'scroll', 0.3, 3, 0.05, x),
      swRow('Natural scrolling', 'natural', 'Content follows your fingers'),
      segRow('Touching the screen', 'touch', [['direct', 'Clicks'], ['trackpad', 'Trackpad']]),
      swRow('Haptics', 'haptics')),
    streamSettings(),
    h('div', { class: 'foot' },
      `Gestures: tap = click · two-finger tap = right-click · hold or tap-then-drag = drag · two fingers = scroll · three-finger tap = middle-click · three-finger swipe ↑ task view, ↓ desktop, ←/→ switch app · pinch the screen to zoom`)) });
}
$('btn-settings').addEventListener('click', () => { haptic(5); settingsSheet(); });

// ============================================================= displays ===
function updateDisplayPill() {
  const p = $('display-pill');
  p.classList.toggle('hidden', displays.length < 2);
  p.textContent = disp().name || 'Display';
}
$('display-pill').addEventListener('click', () => {
  dispIdx = (dispIdx + 1) % displays.length;
  haptic(6);
  updateDisplayPill();
  layout();
  video.sentCfg = '';
  video.sendCfg();
});

// ========================================================= HD (HTTPS) ===
if (!window.isSecureContext && INFO.https) {
  const p = $('hd-pill');
  p.classList.remove('hidden');
  p.addEventListener('click', () => {
    const tok = new URL(url('/')).searchParams.get('token');
    location.href = INFO.https + (tok ? '?token=' + encodeURIComponent(tok) : '');
  });
}

// ============================================================ lifecycle ===
let active = false;
gestures(pad, { direct: false });
gestures(stage, { direct: true });
// The gesture hint goes at the first use of the pad: touch, mouse or wheel.
const hideHint = () => $('pad-hint').classList.add('gone');
pad.addEventListener('pointerdown', hideHint, { once: true });
pad.addEventListener('wheel', hideHint, { once: true, passive: true });
renderStrip();
updateDisplayPill();
bus.addEventListener('settings', e => {
  const k = e.detail.key;
  if (k === 'stats') $('hud').classList.toggle('hidden', !settings.stats);
  if (k === 'hidePanel' && active) layout();
  if (!active || !video.ws) return;
  if (k === 'stream' && JSON.stringify(resolvePlan()) !== JSON.stringify(video.plan)) { video.stop(); video.start(); }
  else if (k === 'quality' || k === 'maxMbps') video.wantConfig();  // debounced, seamless switch
});
document.addEventListener('visibilitychange', () => {
  if (!active) return;
  if (document.hidden) video.stop(); else { video.start(); input.connect(); }
});
export function show() {
  active = true;
  input.connect();
  layout();
  supportReady.then(() => active && video.start());
}
export function hide() {
  active = false;
  document.body.classList.remove('theater', 'theater-b');
  video.stop();
}
hydrateIcons(document.getElementById('view-remote'));
