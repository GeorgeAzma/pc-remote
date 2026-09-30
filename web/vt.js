// A compact xterm-compatible terminal emulator: parser + screen model, no
// DOM. Enough VT for ConPTY, PSReadLine, bash/readline, vim and htop.
// ------------------------------------------------------------ palette ---
const BASE = ['#0c0c0c', '#c50f1f', '#13a10e', '#c19c00', '#0037da', '#881798', '#3a96dd', '#cccccc',
              '#767676', '#e74856', '#16c60c', '#f9f1a5', '#3b78ff', '#b4009e', '#61d6d6', '#f2f2f2'];
export const PAL = BASE.slice();
for (let r = 0; r < 6; r++) for (let g = 0; g < 6; g++) for (let b = 0; b < 6; b++)
  PAL.push('#' + [r, g, b].map(v => (v ? v * 40 + 55 : 0).toString(16).padStart(2, '0')).join(''));
for (let i = 0; i < 24; i++) { const v = (8 + i * 10).toString(16).padStart(2, '0'); PAL.push('#' + v + v + v); }
export const FG0 = '#cccccc', BG0 = '#0c0c0e';
export const F = { bold: 1, dim: 2, italic: 4, under: 8, blink: 16, inverse: 32, hidden: 64, strike: 128 };

// Attributes are interned: each cell stores a small int.
export const attrs = [{ fg: null, bg: null, fl: 0 }], attrKey = new Map([['||0', 0]]);
function attrId(fg, bg, fl) {
  const k = `${fg ?? ''}|${bg ?? ''}|${fl}`;
  let id = attrKey.get(k);
  if (id === undefined) { id = attrs.length; attrs.push({ fg, bg, fl }); attrKey.set(k, id); }
  return id;
}
const DEC_GFX = { j: '┘', k: '┐', l: '┌', m: '└', n: '┼', q: '─', t: '├', u: '┤', v: '┴', w: '┬', x: '│',
  a: '▒', f: '°', g: '±', '~': '·', o: '⎺', p: '⎻', r: '⎼', s: '⎽', '`': '◆', y: '≤', z: '≥', '{': 'π', '|': '≠', '}': '£' };

function wcwidth(cp) {
  if (cp < 0x1100) return 1;
  if ((cp >= 0x1100 && cp <= 0x115f) || (cp >= 0x2e80 && cp <= 0xa4cf && cp !== 0x303f) ||
      (cp >= 0xac00 && cp <= 0xd7a3) || (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0xfe30 && cp <= 0xfe4f) ||
      (cp >= 0xff00 && cp <= 0xff60) || (cp >= 0xffe0 && cp <= 0xffe6) || (cp >= 0x1f300 && cp <= 0x1f64f) ||
      (cp >= 0x1f900 && cp <= 0x1f9ff) || (cp >= 0x20000 && cp <= 0x3fffd)) return 2;
  return 1;
}
const isCombining = cp => (cp >= 0x300 && cp <= 0x36f) || (cp >= 0x200b && cp <= 0x200f) || cp === 0xfe0f ||
  (cp >= 0x1ab0 && cp <= 0x1aff) || (cp >= 0x20d0 && cp <= 0x20ff);

// ------------------------------------------------------------- buffer ---
class Line {
  constructor(cols, a = 0) { this.ch = new Array(cols).fill(' '); this.at = new Array(cols).fill(a); }
  resize(cols) {
    while (this.ch.length < cols) { this.ch.push(' '); this.at.push(0); }
    this.ch.length = cols; this.at.length = cols;
  }
}

export class Emulator {
  constructor(cols, rows, send) {
    this.send = send;
    this.cols = cols; this.rows = rows;
    this.scrollback = [];
    this.maxScroll = 3000;
    this.onScroll = null;   // (line) => void  top line pushed to scrollback
    this.onTitle = null;
    this.reset();
  }
  reset() {
    this.main = this.blank(this.rows); this.alt = null; this.buf = this.main; this.scrollback = [];
    this.x = 0; this.y = 0; this.wrap = false; this.fg = null; this.bg = null; this.fl = 0; this.a = 0;
    this.top = 0; this.bot = this.rows - 1; this.saved = null;
    this.modes = { awm: true, ckm: false, om: false, irm: false, cursor: true, paste: false };
    this.g0 = false; this.lastCh = ' ';
    this.tabs = new Set(); for (let i = 8; i < 500; i += 8) this.tabs.add(i);
    this.state = 0; this.params = ''; this.inter = ''; this.osc = '';
    this.dirty = new Set(); for (let i = 0; i < this.rows; i++) this.dirty.add(i);
    this.all = true;
  }
  blank(n) { return Array.from({ length: n }, () => new Line(this.cols)); }
  mark(y) { this.dirty.add(y); }

  resize(cols, rows) {
    if (cols === this.cols && rows === this.rows) return;
    for (const b of [this.main, this.alt]) if (b) b.forEach(l => l.resize(cols));
    this.scrollback.forEach(l => l.resize(cols));
    this.cols = cols;
    const fit = b => {
      while (b.length > rows) {
        // prefer dropping blank lines below the cursor, else scroll the top off
        if (b === this.buf && this.y < b.length - 1 && b.at(-1).ch.every(c => c === ' ')) { b.pop(); continue; }
        const l = b.shift();
        if (b === this.main) this.pushScroll(l);
        if (b === this.buf) this.y = Math.max(0, this.y - 1);
      }
      while (b.length < rows) b.push(new Line(cols));
    };
    fit(this.main); if (this.alt) fit(this.alt);
    this.rows = rows; this.top = 0; this.bot = rows - 1;
    this.x = Math.min(this.x, cols - 1); this.y = Math.min(this.y, rows - 1);
    this.all = true;
  }
  pushScroll(l) {
    this.scrollback.push(l);
    if (this.scrollback.length > this.maxScroll) this.scrollback.shift();
    this.onScroll && this.onScroll(l);
  }
  scrollUp(n = 1, top = this.top, bot = this.bot, save = true) {
    for (let i = 0; i < n; i++) {
      const l = this.buf.splice(top, 1)[0];
      if (save && this.buf === this.main && top === 0 && !this.alt) this.pushScroll(l);
      this.buf.splice(bot, 0, new Line(this.cols, this.bgAttr()));
    }
    this.all = true;
  }
  scrollDown(n = 1, top = this.top, bot = this.bot) {
    for (let i = 0; i < n; i++) {
      this.buf.splice(bot, 1);
      this.buf.splice(top, 0, new Line(this.cols, this.bgAttr()));
    }
    this.all = true;
  }
  bgAttr() { return this.bg === null ? 0 : attrId(null, this.bg, 0); }
  lf() {
    if (this.y === this.bot) this.scrollUp();
    else if (this.y < this.rows - 1) this.y++;
  }
  setAttr() { this.a = attrId(this.fg, this.bg, this.fl); }

  // ----------------------------------------------------------- input ---
  write(str) {
    for (const ch of str) {
      const cp = ch.codePointAt(0);
      switch (this.state) {
        case 0:  // ground
          if (cp === 0x1b) { this.state = 1; this.inter = ''; }
          else if (cp < 0x20 || cp === 0x7f) this.control(cp);
          else this.print(ch, cp);
          break;
        case 1:  // ESC
          if (ch === '[') { this.state = 2; this.params = ''; this.inter = ''; }
          else if (ch === ']') { this.state = 3; this.osc = ''; }
          else if (ch === 'P' || ch === '_' || ch === '^' || ch === 'X') { this.state = 5; }
          else if (ch === '(' || ch === ')' || ch === '*' || ch === '+') { this.state = 6; this.inter = ch; }
          else if (ch === '#' || ch === '%' || ch === ' ') { this.state = 7; }
          else { this.esc(ch); this.state = 0; }
          break;
        case 2:  // CSI
          if (cp >= 0x30 && cp <= 0x3f) this.params += ch;
          else if (cp >= 0x20 && cp <= 0x2f) this.inter += ch;
          else if (cp >= 0x40 && cp <= 0x7e) { this.csi(ch); this.state = 0; }
          else if (cp === 0x1b) { this.state = 1; }
          else if (cp < 0x20) this.control(cp);
          else this.state = 0;
          break;
        case 3:  // OSC ... BEL | ESC \
          if (cp === 0x07) { this.oscDone(); this.state = 0; }
          else if (cp === 0x1b) { this.state = 4; }
          else if (this.osc.length < 4096) this.osc += ch;
          break;
        case 4: this.oscDone(); this.state = ch === '\\' ? 0 : 1; if (this.state === 1) this.write(ch); break;
        case 5:  // DCS/APC/PM/SOS: swallow until ST. Any ESC ends the string
          // (VT500 parser model); the '\' of ST is then a no-op escape.
          if (cp === 0x1b) this.state = 1; else if (cp === 0x07) this.state = 0;
          break;
        case 6: if (this.inter === '(') this.g0 = ch === '0'; this.state = 0; break;
        case 7: this.state = 0; break;
      }
    }
  }
  control(cp) {
    switch (cp) {
      case 0x07: break;  // BEL
      case 0x08: if (this.x > 0) this.x--; this.wrap = false; break;
      case 0x09: { let x = this.x + 1; while (x < this.cols - 1 && !this.tabs.has(x)) x++; this.x = Math.min(x, this.cols - 1); this.wrap = false; break; }
      case 0x0a: case 0x0b: case 0x0c: this.lf(); this.wrap = false; break;
      case 0x0d: this.x = 0; this.wrap = false; break;
      case 0x0e: case 0x0f: break;
    }
    this.mark(this.y);
  }
  print(ch, cp) {
    if (this.g0 && DEC_GFX[ch]) ch = DEC_GFX[ch];
    if (isCombining(cp)) {  // attach to the previous cell
      const px = this.wrap ? this.x : Math.max(0, this.x - 1);
      this.buf[this.y].ch[px] += ch; this.mark(this.y); return;
    }
    const w = wcwidth(cp);
    if (this.wrap && this.modes.awm) { this.x = 0; this.lf(); this.wrap = false; }
    if (w === 2 && this.x === this.cols - 1) { this.buf[this.y].ch[this.x] = ' '; if (this.modes.awm) { this.x = 0; this.lf(); } }
    const line = this.buf[this.y];
    if (this.modes.irm) {
      line.ch.splice(this.x, 0, ...(w === 2 ? [ch, ''] : [ch])); line.at.splice(this.x, 0, ...(w === 2 ? [this.a, this.a] : [this.a]));
      line.ch.length = this.cols; line.at.length = this.cols;
    } else {
      line.ch[this.x] = ch; line.at[this.x] = this.a;
      if (w === 2 && this.x + 1 < this.cols) { line.ch[this.x + 1] = ''; line.at[this.x + 1] = this.a; }
    }
    this.lastCh = ch;
    this.mark(this.y);
    this.x += w;
    if (this.x >= this.cols) { this.x = this.cols - 1; this.wrap = true; }
  }
  esc(ch) {
    switch (ch) {
      case '7': this.saveCursor(); break;
      case '8': this.restoreCursor(); break;
      case 'D': this.lf(); break;
      case 'E': this.x = 0; this.lf(); break;
      case 'M': if (this.y === this.top) this.scrollDown(); else if (this.y > 0) this.y--; break;
      case 'H': this.tabs.add(this.x); break;
      case 'c': this.reset(); break;
    }
    this.wrap = false;
  }
  saveCursor() { this.saved = { x: this.x, y: this.y, fg: this.fg, bg: this.bg, fl: this.fl, g0: this.g0, om: this.modes.om }; }
  restoreCursor() {
    const s = this.saved || { x: 0, y: 0, fg: null, bg: null, fl: 0, g0: false, om: false };
    Object.assign(this, { x: Math.min(s.x, this.cols - 1), y: Math.min(s.y, this.rows - 1), fg: s.fg, bg: s.bg, fl: s.fl, g0: s.g0 });
    this.modes.om = s.om; this.setAttr(); this.wrap = false;
  }
  oscDone() {
    const i = this.osc.indexOf(';');
    const code = i < 0 ? this.osc : this.osc.slice(0, i);
    if ((code === '0' || code === '2') && this.onTitle) this.onTitle(this.osc.slice(i + 1));
  }
  clearLine(y, x0, x1) {
    const l = this.buf[y], a = this.bgAttr();
    for (let x = x0; x < x1; x++) { l.ch[x] = ' '; l.at[x] = a; }
    this.mark(y);
  }
  csi(fin) {
    const priv = this.params[0] === '?' || this.params[0] === '>' || this.params[0] === '=' ? this.params[0] : '';
    const ps = (priv ? this.params.slice(1) : this.params).split(';').map(v => v === '' ? NaN : parseInt(v.split(':')[0], 10));
    const p = (i, d = 1) => (isNaN(ps[i]) || ps[i] === 0 ? d : ps[i]);
    const rows = this.rows, cols = this.cols;
    this.mark(this.y);
    if (this.inter === ' ' && fin === 'q') return;  // cursor style
    if (this.inter === '!' && fin === 'p') { this.fg = this.bg = null; this.fl = 0; this.setAttr(); this.modes.irm = false; this.modes.om = false; this.top = 0; this.bot = rows - 1; return; }
    switch (fin) {
      case 'A': this.y = Math.max(this.y < this.top ? 0 : this.top, this.y - p(0)); break;
      case 'B': case 'e': this.y = Math.min(this.y > this.bot ? rows - 1 : this.bot, this.y + p(0)); break;
      case 'C': case 'a': this.x = Math.min(cols - 1, this.x + p(0)); break;
      case 'D': this.x = Math.max(0, this.x - p(0)); break;
      case 'E': this.x = 0; this.y = Math.min(this.bot, this.y + p(0)); break;
      case 'F': this.x = 0; this.y = Math.max(this.top, this.y - p(0)); break;
      case 'G': case '`': this.x = Math.min(cols - 1, p(0) - 1); break;
      case 'd': this.y = Math.min(rows - 1, (this.modes.om ? this.top : 0) + p(0) - 1); break;
      case 'H': case 'f': {
        const oy = this.modes.om ? this.top : 0;
        this.y = Math.min(this.modes.om ? this.bot : rows - 1, oy + p(0) - 1);
        this.x = Math.min(cols - 1, p(1) - 1);
        break;
      }
      case 'J': {
        const m = isNaN(ps[0]) ? 0 : ps[0];
        if (m === 0) { this.clearLine(this.y, this.x, cols); for (let y = this.y + 1; y < rows; y++) this.clearLine(y, 0, cols); }
        else if (m === 1) { this.clearLine(this.y, 0, this.x + 1); for (let y = 0; y < this.y; y++) this.clearLine(y, 0, cols); }
        else if (m === 2) { for (let y = 0; y < rows; y++) this.clearLine(y, 0, cols); }
        else if (m === 3) { this.scrollback = []; this.onClearScroll && this.onClearScroll(); }
        break;
      }
      case 'K': {
        const m = isNaN(ps[0]) ? 0 : ps[0];
        if (m === 0) this.clearLine(this.y, this.x, cols); else if (m === 1) this.clearLine(this.y, 0, this.x + 1); else this.clearLine(this.y, 0, cols);
        break;
      }
      case 'X': this.clearLine(this.y, this.x, Math.min(cols, this.x + p(0))); break;
      case 'P': { const l = this.buf[this.y], n = Math.min(p(0), cols - this.x);
        l.ch.splice(this.x, n); l.at.splice(this.x, n);
        for (let i = 0; i < n; i++) { l.ch.push(' '); l.at.push(this.bgAttr()); } break; }
      case '@': { const l = this.buf[this.y], n = Math.min(p(0), cols - this.x);
        l.ch.splice(this.x, 0, ...Array(n).fill(' ')); l.at.splice(this.x, 0, ...Array(n).fill(this.bgAttr()));
        l.ch.length = cols; l.at.length = cols; break; }
      case 'L': if (this.y >= this.top && this.y <= this.bot) this.scrollDown(p(0), this.y, this.bot); break;
      case 'M': if (this.y >= this.top && this.y <= this.bot) this.scrollUp(p(0), this.y, this.bot, false); break;  // DL deletes, it doesn't scroll off
      case 'S': if (!priv) this.scrollUp(p(0)); break;
      case 'T': if (!priv) this.scrollDown(p(0)); break;
      case 'b': for (let i = 0; i < Math.min(p(0), 2000); i++) this.print(this.lastCh, this.lastCh.codePointAt(0)); break;
      case 'g': if (!ps[0]) this.tabs.delete(this.x); else if (ps[0] === 3) this.tabs.clear(); break;
      case 'r': if (!priv) {
        const t = p(0) - 1, b = (isNaN(ps[1]) ? rows : ps[1]) - 1;
        if (t < b && b < rows) { this.top = t; this.bot = b; this.x = 0; this.y = this.modes.om ? t : 0; }
      } break;
      case 's': if (!priv) this.saveCursor(); break;
      case 'u': if (!priv) this.restoreCursor(); break;
      case 'm': if (!priv) this.sgr(ps); break;
      case 'h': case 'l': this.setModes(priv, ps, fin === 'h'); break;
      case 'n':
        if (ps[0] === 5) this.send('\x1b[0n');
        else if (ps[0] === 6) this.send(`\x1b[${this.y + 1};${this.x + 1}R`);
        break;
      case 'c': this.send(priv === '>' ? '\x1b[>0;10;1c' : '\x1b[?1;2c'); break;
      case 't': break;  // window ops (ConPTY reports resizes this way)
    }
    this.wrap = false;
    this.mark(this.y);
  }
  setModes(priv, ps, on) {
    for (const m of ps) {
      if (!priv) { if (m === 4) this.modes.irm = on; continue; }
      switch (m) {
        case 1: this.modes.ckm = on; break;
        case 6: this.modes.om = on; this.x = 0; this.y = on ? this.top : 0; break;
        case 7: this.modes.awm = on; break;
        case 25: this.modes.cursor = on; this.mark(this.y); break;
        case 2004: this.modes.paste = on; break;
        case 47: case 1047: case 1049:
          if (on && !this.alt) {
            if (m === 1049) this.saveCursor();
            this.alt = this.blank(this.rows); this.buf = this.alt; this.all = true;
          } else if (!on && this.alt) {
            this.alt = null; this.buf = this.main; this.all = true;
            if (m === 1049) this.restoreCursor();
          }
          break;
      }
    }
  }
  sgr(ps) {
    if (!ps.length) ps = [0];
    for (let i = 0; i < ps.length; i++) {
      const c = isNaN(ps[i]) ? 0 : ps[i];
      if (c === 0) { this.fg = this.bg = null; this.fl = 0; }
      else if (c === 1) this.fl |= F.bold; else if (c === 2) this.fl |= F.dim; else if (c === 3) this.fl |= F.italic;
      else if (c === 4) this.fl |= F.under; else if (c === 5 || c === 6) this.fl |= F.blink; else if (c === 7) this.fl |= F.inverse;
      else if (c === 8) this.fl |= F.hidden; else if (c === 9) this.fl |= F.strike;
      else if (c === 21 || c === 22) this.fl &= ~(F.bold | F.dim); else if (c === 23) this.fl &= ~F.italic;
      else if (c === 24) this.fl &= ~F.under; else if (c === 25) this.fl &= ~F.blink; else if (c === 27) this.fl &= ~F.inverse;
      else if (c === 28) this.fl &= ~F.hidden; else if (c === 29) this.fl &= ~F.strike;
      else if (c >= 30 && c <= 37) this.fg = c - 30; else if (c === 39) this.fg = null;
      else if (c >= 40 && c <= 47) this.bg = c - 40; else if (c === 49) this.bg = null;
      else if (c >= 90 && c <= 97) this.fg = c - 90 + 8; else if (c >= 100 && c <= 107) this.bg = c - 100 + 8;
      else if (c === 38 || c === 48) {
        let col = null;
        if (ps[i + 1] === 5) { col = ps[i + 2] | 0; i += 2; }
        else if (ps[i + 1] === 2) { col = '#' + [ps[i + 2], ps[i + 3], ps[i + 4]].map(v => ((v | 0) & 255).toString(16).padStart(2, '0')).join(''); i += 4; }
        if (c === 38) this.fg = col; else this.bg = col;
      }
    }
    this.setAttr();
  }
}
