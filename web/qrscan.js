// A QR code reader for photos and camera frames, for signing in by
// scanning the PC's pairing code: the three finder squares, a perspective
// correction (through the alignment pattern), the grid, Reed-Solomon error
// correction. Reads what qr.js writes: error correction level M, versions
// 1-10. Self-contained, like the encoder; the browser's own BarcodeDetector
// is used first where there is one (Chrome on Android).
import { EC_M, EXP, LOG, mul, MASKS, formatBits, formatSpots, dataOrder, functionModules } from './qr.js';

// ------------------------------------------------------------- binarize ---
// Block-wise threshold (8x8 blocks, averaged over their 5x5 neighbourhood),
// so uneven light across a photographed screen doesn't matter.
function binarize(gray, w, h) {
  const B = 8, bw = Math.ceil(w / B), bh = Math.ceil(h / B), black = new Float32Array(bw * bh);
  for (let by = 0; by < bh; by++) for (let bx = 0; bx < bw; bx++) {
    let min = 255, max = 0, sum = 0, n = 0;
    for (let y = by * B; y < Math.min(h, by * B + B); y++) for (let x = bx * B; x < Math.min(w, bx * B + B); x++) {
      const v = gray[y * w + x];
      sum += v; n++;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    let avg = sum / n;
    if (max - min <= 24) {  // flat block: dark only if darker than its neighbours
      avg = min / 2;
      if (bx > 0 && by > 0) {
        const nb = (black[(by - 1) * bw + bx] + 2 * black[by * bw + bx - 1] + black[(by - 1) * bw + bx - 1]) / 4;
        if (min < nb) avg = nb;
      }
    }
    black[by * bw + bx] = avg;
  }
  const bits = new Uint8Array(w * h);
  for (let by = 0; by < bh; by++) for (let bx = 0; bx < bw; bx++) {
    let sum = 0;
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) {
      const yy = Math.min(bh - 1, Math.max(0, by + dy)), xx = Math.min(bw - 1, Math.max(0, bx + dx));
      sum += black[yy * bw + xx];
    }
    const t = sum / 25;
    for (let y = by * B; y < Math.min(h, by * B + B); y++) for (let x = bx * B; x < Math.min(w, bx * B + B); x++)
      bits[y * w + x] = gray[y * w + x] <= t ? 1 : 0;
  }
  return bits;
}

// -------------------------------------------------------- finder squares ---
// A finder square crossed through its centre reads dark, light, dark, light,
// dark in the ratio 1:1:3:1:1, in any direction.
function ratioOk(c, loose = 1) {
  const total = c[0] + c[1] + c[2] + c[3] + c[4];
  if (total < 7) return false;
  const m = total / 7, v = m * 0.5 * loose;
  return Math.abs(c[0] - m) < v && Math.abs(c[1] - m) < v && Math.abs(c[2] - 3 * m) < 3 * v &&
    Math.abs(c[3] - m) < v && Math.abs(c[4] - m) < v;
}

// Runs along a line through (cx, cy) in direction (dx, dy): the 5 run lengths
// and the centre's offset along the line, or null.
function crossCheck(bits, w, h, cx, cy, dx, dy, maxCount) {
  const at = (k) => { const x = Math.round(cx + dx * k), y = Math.round(cy + dy * k); return x >= 0 && y >= 0 && x < w && y < h ? bits[y * w + x] : -1; };
  const c = [0, 0, 0, 0, 0];
  let k = 0;
  while (at(k) === 1) { c[2]++; k--; }
  while (at(k) === 0 && c[1] <= maxCount) { c[1]++; k--; }
  if (at(k) !== 1 || c[1] > maxCount) return null;
  while (at(k) === 1 && c[0] <= maxCount) { c[0]++; k--; }
  if (c[0] > maxCount) return null;
  k = 1;
  while (at(k) === 1) { c[2]++; k++; }
  while (at(k) === 0 && c[3] <= maxCount) { c[3]++; k++; }
  if (at(k) !== 1 || c[3] > maxCount) return null;
  while (at(k) === 1 && c[4] <= maxCount) { c[4]++; k++; }
  if (c[4] > maxCount) return null;
  if (!ratioOk(c, 1.4)) return null;
  const end = k;  // one past the last dark module of the far edge
  return { c, center: end - c[4] - c[3] - c[2] / 2 - 0.5 };
}

function findFinders(bits, w, h) {
  const found = [];
  const add = (x, y, size) => {
    for (const f of found) {
      if (Math.hypot(f.x - x, f.y - y) < Math.max(f.size, size) * 2 && Math.abs(f.size - size) < Math.max(f.size, size) * 0.6) {
        f.x = (f.x * f.n + x) / (f.n + 1); f.y = (f.y * f.n + y) / (f.n + 1); f.size = (f.size * f.n + size) / (f.n + 1); f.n++;
        return;
      }
    }
    found.push({ x, y, size, n: 1 });
  };
  const step = Math.max(1, Math.floor(h / 180));
  for (let y = 0; y < h; y += step) {
    // runs along the row
    const runs = [];
    let x = 0;
    while (x < w) {
      const v = bits[y * w + x], s = x;
      while (x < w && bits[y * w + x] === v) x++;
      runs.push({ v, s, len: x - s });
    }
    for (let i = 0; i + 4 < runs.length; i++) {
      if (runs[i].v !== 1) continue;
      const c = [runs[i].len, runs[i + 1].len, runs[i + 2].len, runs[i + 3].len, runs[i + 4].len];
      if (!ratioOk(c)) continue;
      const total = c.reduce((a, b) => a + b, 0);
      let cx = runs[i + 2].s + runs[i + 2].len / 2 - 0.5;
      const v = crossCheck(bits, w, h, cx, y, 0, 1, c[2] * 2);
      if (!v) continue;
      const cy = y + v.center;
      const hz = crossCheck(bits, w, h, cx, cy, 1, 0, c[2] * 2);
      if (!hz) continue;
      cx += hz.center;
      const vt = v.c.reduce((a, b) => a + b, 0), ht = hz.c.reduce((a, b) => a + b, 0);
      if (Math.abs(vt - total) > total * 0.6 || Math.abs(ht - total) > total * 0.6) continue;
      // and diagonally, which rules out most things that merely look striped
      if (!crossCheck(bits, w, h, cx, cy, Math.SQRT1_2, Math.SQRT1_2, c[2] * 3)) continue;
      add(cx, cy, (total + vt + ht) / 21);
    }
  }
  return found;
}

// Three of the candidates as top-left / top-right / bottom-left, best first:
// two equal sides at a right angle, similar module sizes.
function triples(found) {
  const cand = [...found].sort((a, b) => b.n - a.n).slice(0, 8), out = [];
  for (let i = 0; i < cand.length; i++) for (let j = i + 1; j < cand.length; j++) for (let k = j + 1; k < cand.length; k++) {
    const p = [cand[i], cand[j], cand[k]];
    const sizes = p.map(q => q.size), ms = (sizes[0] + sizes[1] + sizes[2]) / 3;
    if (Math.max(...sizes) > Math.min(...sizes) * 1.8) continue;
    const d = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
    const sides = [[d(p[1], p[2]), 0], [d(p[0], p[2]), 1], [d(p[0], p[1]), 2]].sort((a, b) => a[0] - b[0]);
    const [s1, s2, s3] = sides.map(s => s[0]);
    if (s1 < ms * 7) continue;
    const score = Math.abs(s1 - s2) / s2 + Math.abs(s3 - Math.hypot(s1, s2)) / s3 + (Math.max(...sizes) - Math.min(...sizes)) / ms * 0.5;
    if (score > 0.5) continue;
    // the corner opposite the longest side is the top-left one
    const tl = p[sides[2][1]], [a, c] = p.filter(q => q !== tl);
    const cross = (c.x - tl.x) * (a.y - tl.y) - (c.y - tl.y) * (a.x - tl.x);
    const [bl, tr] = cross < 0 ? [c, a] : [a, c];
    out.push({ tl, tr, bl, size: ms, score: score - (p[0].n + p[1].n + p[2].n) * 0.002 });  // (seen on more rows: likelier real)
  }
  return out.sort((a, b) => a.score - b.score);
}

// ------------------------------------------------------------- geometry ---
// The perspective transform taking 4 points to 4 points (a 3x3 matrix).
function homography(src, dst) {
  const A = [], b = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = src[i], [X, Y] = dst[i];
    A.push([x, y, 1, 0, 0, 0, -X * x, -X * y]); b.push(X);
    A.push([0, 0, 0, x, y, 1, -Y * x, -Y * y]); b.push(Y);
  }
  for (let c = 0; c < 8; c++) {  // Gaussian elimination
    let p = c;
    for (let r = c + 1; r < 8; r++) if (Math.abs(A[r][c]) > Math.abs(A[p][c])) p = r;
    [A[c], A[p]] = [A[p], A[c]]; [b[c], b[p]] = [b[p], b[c]];
    if (Math.abs(A[c][c]) < 1e-12) return null;
    for (let r = 0; r < 8; r++) {
      if (r === c) continue;
      const f = A[r][c] / A[c][c];
      for (let k = c; k < 8; k++) A[r][k] -= f * A[c][k];
      b[r] -= f * b[c];
    }
  }
  const hm = b.map((v, i) => v / A[i][i]);
  return (x, y) => {
    const z = hm[6] * x + hm[7] * y + 1;
    return [(hm[0] * x + hm[1] * y + hm[2]) / z, (hm[3] * x + hm[4] * y + hm[5]) / z];
  };
}

// The bottom-right alignment pattern (versions 2+) near where the finders
// say it should be: the best match for its 5x5 ring pattern.
function findAlignment(bits, w, h, t, dim) {
  // module steps across and down, scaled to the bottom-right corner (under
  // perspective the far corner is smaller: as small as the finders beside it)
  const su = t.bl.size / t.tl.size, sv = t.tr.size / t.tl.size;
  const u = [(t.tr.x - t.tl.x) / (dim - 7) * su, (t.tr.y - t.tl.y) / (dim - 7) * su];
  const v = [(t.bl.x - t.tl.x) / (dim - 7) * sv, (t.bl.y - t.tl.y) / (dim - 7) * sv];
  const k = (dim - 10) / (dim - 7);  // alignment centre at module dim-6.5, finders at 3.5
  const ex = t.tl.x + k * ((t.tr.x - t.tl.x) * su + (t.bl.x - t.tl.x) * sv);
  const ey = t.tl.y + k * ((t.tr.y - t.tl.y) * su + (t.bl.y - t.tl.y) * sv);
  const px = (x, y) => { x = Math.round(x); y = Math.round(y); return x >= 0 && y >= 0 && x < w && y < h ? bits[y * w + x] : 0; };
  const want = (i, j) => (Math.max(Math.abs(i), Math.abs(j)) !== 1 ? 1 : 0);
  const r = Math.ceil(Math.hypot(...u) * 6 + Math.hypot(...v) * 6), hits = [];
  let top = 0;
  for (let y = Math.round(ey - r); y <= ey + r; y++) for (let x = Math.round(ex - r); x <= ex + r; x++) {
    if (!px(x, y)) continue;
    let s = 0;
    for (let j = -2; j <= 2; j++) for (let i = -2; i <= 2; i++) s += px(x + i * u[0] + j * v[0], y + i * u[1] + j * v[1]) === want(i, j) ? 1 : 0;
    if (s >= top) { if (s > top) hits.length = 0; top = s; hits.push([x, y]); }
  }
  if (top < 22) return null;
  // the best match nearest the estimate, averaged with its neighbours (a module's worth of equally good spots)
  const near = hits.reduce((a, p) => (Math.hypot(p[0] - ex, p[1] - ey) < Math.hypot(a[0] - ex, a[1] - ey) ? p : a));
  const around = hits.filter(p => Math.hypot(p[0] - near[0], p[1] - near[1]) <= Math.hypot(...u));
  return [around.reduce((a, p) => a + p[0], 0) / around.length, around.reduce((a, p) => a + p[1], 0) / around.length];
}

function sampleGrid(bits, w, h, t, dim) {
  const src = [[3.5, 3.5], [dim - 3.5, 3.5], [3.5, dim - 3.5]], dst = [[t.tl.x, t.tl.y], [t.tr.x, t.tr.y], [t.bl.x, t.bl.y]];
  const al = dim > 21 ? findAlignment(bits, w, h, t, dim) : null;
  if (al) { src.push([dim - 6.5, dim - 6.5]); dst.push(al); } else {  // a parallelogram, no perspective
    src.push([dim - 3.5, dim - 3.5]); dst.push([t.tr.x + t.bl.x - t.tl.x, t.tr.y + t.bl.y - t.tl.y]);
  }
  const map = homography(src, dst);
  if (!map) return null;
  const m = [];
  for (let y = 0; y < dim; y++) {
    const row = [];
    for (let x = 0; x < dim; x++) {
      const [ix, iy] = map(x + 0.5, y + 0.5), X = Math.round(ix), Y = Math.round(iy);
      row.push(X >= 0 && Y >= 0 && X < w && Y < h ? bits[Y * w + X] === 1 : false);
    }
    m.push(row);
  }
  return m;
}

// --------------------------------------------------------- Reed-Solomon ---
const div = (a, b) => (a ? EXP[(LOG[a] + 255 - LOG[b]) % 255] : 0);
// Corrects one block (data + error correction codewords) in place; false if it can't.
function rsCorrect(block, ec) {
  const n = block.length, S = [];
  let bad = false;
  for (let j = 0; j < ec; j++) {
    let s = 0;
    for (const c of block) s = mul(s, EXP[j]) ^ c;
    S.push(s);
    if (s) bad = true;
  }
  if (!bad) return true;
  // Berlekamp-Massey: the error locator (lowest degree first)
  let C = [1], B = [1], L = 0, m = 1, b = 1;
  for (let k = 0; k < ec; k++) {
    let d = S[k];
    for (let i = 1; i <= L; i++) d ^= mul(C[i] || 0, S[k - i]);
    if (!d) { m++; continue; }
    const coef = div(d, b), T = C.slice(), next = C.slice();
    for (let i = 0; i < B.length; i++) next[i + m] = (next[i + m] || 0) ^ mul(coef, B[i]);
    for (let i = 0; i < next.length; i++) next[i] = next[i] || 0;
    C = next;
    if (2 * L <= k) { L = k + 1 - L; B = T; b = d; m = 1; } else m++;
  }
  if (2 * L > ec) return false;
  const evalAt = (P, x) => { let r = 0, p = 1; for (const c of P) { r ^= mul(c, p); p = mul(p, x); } return r; };
  // error evaluator: S(x) * C(x) mod x^ec
  const O = new Array(ec).fill(0);
  for (let i = 0; i < ec; i++) for (let j = 0; j < C.length && i + j < ec; j++) O[i + j] ^= mul(S[i], C[j]);
  const dC = C.map((c, i) => (i % 2 ? c : 0)).slice(1);  // formal derivative
  let fixed = 0;
  for (let p = 0; p < n; p++) {  // Chien search: position p counts from the end
    const xinv = EXP[(255 - p) % 255];
    if (evalAt(C, xinv)) continue;
    const den = evalAt(dC, xinv);
    if (!den) return false;
    block[n - 1 - p] ^= mul(EXP[p % 255], div(evalAt(O, xinv), den));
    fixed++;
  }
  return fixed === L;
}

// ---------------------------------------------------------------- decode ---
function readFormat(m) {
  const size = m.length;
  const read = copy => { let v = 0; for (let i = 0; i < 15; i++) { const [x, y] = formatSpots(size, i)[copy]; if (m[y][x]) v |= 1 << i; } return v; };
  const got = [read(0), read(1)];
  let best = null;
  for (let mask = 0; mask < 8; mask++) {
    const code = formatBits(mask);  // level M only: that's what qr.js writes
    for (const g of got) {
      let d = 0;
      for (let x = code ^ g; x; x &= x - 1) d++;
      if (!best || d < best.d) best = { d, mask };
    }
  }
  return best.d <= 3 ? best.mask : -1;
}

function decodeMatrix(m) {
  const size = m.length, version = (size - 17) / 4;
  if (!Number.isInteger(version) || version < 1 || version > 10) return null;
  const mask = readFormat(m);
  if (mask < 0) return null;
  const [ec, ...groups] = EC_M[version], flip = MASKS[mask];
  const lens = groups.flatMap(([n, d]) => new Array(n).fill(d)), total = lens.reduce((a, b) => a + b, 0) + ec * lens.length;
  const order = dataOrder(size, functionModules(version));
  const words = [];
  for (let i = 0; i < total; i++) {
    let v = 0;
    for (let k = 0; k < 8; k++) { const [x, y] = order[i * 8 + k]; v = (v << 1) | ((m[y][x] !== flip(x, y)) ? 1 : 0); }
    words.push(v);
  }
  // un-interleave: data codewords round-robin over the blocks, then error correction
  const blocks = lens.map(() => []);
  let at = 0;
  for (let i = 0; i < Math.max(...lens); i++) lens.forEach((l, b) => { if (i < l) blocks[b].push(words[at++]); });
  for (let i = 0; i < ec; i++) blocks.forEach(b => b.push(words[at++]));
  const data = [];
  for (let b = 0; b < blocks.length; b++) {
    if (!rsCorrect(blocks[b], ec)) return null;
    data.push(...blocks[b].slice(0, lens[b]));
  }
  // the bit stream: segments of numeric / alphanumeric / byte data
  let pos = 0;
  const take = n => { let v = 0; for (let i = 0; i < n; i++, pos++) v = (v << 1) | ((data[pos >>> 3] >>> (7 - (pos & 7))) & 1); return v; };
  const bytes = [], left = () => data.length * 8 - pos;
  const ALNUM = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:';
  while (left() >= 4) {
    const mode = take(4);
    if (mode === 0) break;
    if (mode === 4) { const n = take(version < 10 ? 8 : 16); if (left() < n * 8) return null; for (let i = 0; i < n; i++) bytes.push(take(8)); }
    else if (mode === 1) {
      let n = take(version < 10 ? 10 : 12);
      for (; n >= 3; n -= 3) bytes.push(...new TextEncoder().encode(String(take(10)).padStart(3, '0')));
      if (n) bytes.push(...new TextEncoder().encode(String(take(n === 2 ? 7 : 4)).padStart(n, '0')));
    } else if (mode === 2) {
      let n = take(version < 10 ? 9 : 11);
      for (; n >= 2; n -= 2) { const v = take(11); bytes.push(ALNUM.charCodeAt(Math.floor(v / 45)), ALNUM.charCodeAt(v % 45)); }
      if (n) bytes.push(ALNUM.charCodeAt(take(6)));
    } else if (mode === 7) take(8);  // ECI: assume UTF-8 anyway
    else return null;
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
}

// A finder's module size measured along the line to another finder: along
// the grid, so right under rotation (across a row it'd be up to 1.4x off).
function moduleAlong(bits, w, h, a, b) {
  const d = Math.hypot(b.x - a.x, b.y - a.y), r = crossCheck(bits, w, h, a.x, a.y, (b.x - a.x) / d, (b.y - a.y) / d, a.size * 6);
  return r ? r.c.reduce((x, y) => x + y, 0) / 7 : a.size;
}

function scanBits(bits, w, h) {
  for (const t of triples(findFinders(bits, w, h)).slice(0, 6)) {
    const across = (moduleAlong(bits, w, h, t.tl, t.tr) + moduleAlong(bits, w, h, t.tr, t.tl)) / 2;
    const down = (moduleAlong(bits, w, h, t.tl, t.bl) + moduleAlong(bits, w, h, t.bl, t.tl)) / 2;
    const est = (Math.hypot(t.tr.x - t.tl.x, t.tr.y - t.tl.y) / across + Math.hypot(t.bl.x - t.tl.x, t.bl.y - t.tl.y) / down) / 2 + 7;
    const base = Math.round((est - 17) / 4) * 4 + 17;
    for (const dim of [base, base + 4, base - 4]) {
      if (dim < 21 || dim > 57) continue;
      const m = sampleGrid(bits, w, h, t, dim);
      if (!m) continue;
      const text = decodeMatrix(m) ?? decodeMatrix(m[0].map((_, x) => m.map(r => r[x])));  // (mirrored)
      if (text !== null) return text;
    }
  }
  return null;
}

/** {data (RGBA), width, height} -> the QR code's text, or null. */
export function scanQR(img) {
  const { data, width: w, height: h } = img, gray = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) gray[i] = (data[i * 4] * 77 + data[i * 4 + 1] * 150 + data[i * 4 + 2] * 29) >> 8;
  const text = scanBits(binarize(gray, w, h), w, h);
  if (text !== null) return text;
  // grainy (a dim room, a cheap camera): again, smoothed over 3x3 pixels
  const soft = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    let s = 0, n = 0;
    for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
      const xx = x + dx, yy = y + dy;
      if (xx >= 0 && yy >= 0 && xx < w && yy < h) { s += gray[yy * w + xx]; n++; }
    }
    soft[y * w + x] = s / n;
  }
  return scanBits(binarize(soft, w, h), w, h);
}

// --------------------------------------------------------- in the browser ---
let detector;
/** A video frame (or an image) -> the QR code's text, or null. It's read at
 *  most `max` pixels across: plenty for a QR code, and quick. */
export async function scanSource(src, sw, sh, max = 720) {
  if (detector === undefined) {
    try { detector = 'BarcodeDetector' in window && (await BarcodeDetector.getSupportedFormats()).includes('qr_code')
      ? new BarcodeDetector({ formats: ['qr_code'] }) : null; } catch { detector = null; }
  }
  if (detector) {
    try { const r = await detector.detect(src); if (r[0]) return r[0].rawValue; } catch { /* fall through */ }
  }
  const k = Math.min(1, max / Math.max(sw, sh)), w = Math.round(sw * k), h = Math.round(sh * k);
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(src, 0, 0, w, h);
  return scanQR(ctx.getImageData(0, 0, w, h));
}
