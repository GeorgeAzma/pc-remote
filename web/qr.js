// A small QR code encoder (byte mode, error correction level M, versions
// 1-10: up to 213 bytes, plenty for a pairing link). Self-contained so the
// app works on an offline network. Follows ISO/IEC 18004.

// Error correction per version (level M): [EC codewords per block, [blocks, data codewords]...]
const EC_M = [null,
  [10, [1, 16]], [16, [1, 28]], [26, [1, 44]], [18, [2, 32]], [24, [2, 43]],
  [16, [4, 27]], [18, [4, 31]], [22, [2, 38], [2, 39]], [22, [3, 36], [2, 37]], [26, [4, 43], [1, 44]]];
const ALIGN = [null, [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50]];

// ---- Reed-Solomon over GF(256), polynomial 0x11D
const EXP = new Uint8Array(512), LOG = new Uint8Array(256);
for (let i = 0, x = 1; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 256) x ^= 0x11D; }
for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
const mul = (a, b) => (a && b ? EXP[LOG[a] + LOG[b]] : 0);
function rsGenerator(n) {
  let g = [1];
  for (let i = 0; i < n; i++) {  // g *= (x - a^i)
    const next = new Array(g.length + 1).fill(0);
    g.forEach((c, j) => { next[j] ^= c; next[j + 1] ^= mul(c, EXP[i]); });
    g = next;
  }
  return g;
}
function rsRemainder(data, n) {
  const g = rsGenerator(n), r = new Array(n).fill(0);
  for (const d of data) {
    const f = d ^ r.shift();
    r.push(0);
    for (let i = 0; i < n; i++) r[i] ^= mul(g[i + 1], f);
  }
  return r;
}

function codewords(bytes) {
  for (let v = 1; v <= 10; v++) {
    const [ec, ...groups] = EC_M[v];
    const dataLen = groups.reduce((a, [n, d]) => a + n * d, 0);
    const countBits = v < 10 ? 8 : 16;
    if (4 + countBits + bytes.length * 8 > dataLen * 8) continue;
    // bit stream: mode (byte), count, data, terminator, padding
    const bits = [];
    const put = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    put(4, 4); put(bytes.length, countBits);
    for (const b of bytes) put(b, 8);
    put(0, Math.min(4, dataLen * 8 - bits.length));
    while (bits.length % 8) bits.push(0);
    const data = [];
    for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((a, b) => a * 2 + b, 0));
    for (let pad = 0xEC; data.length < dataLen; pad ^= 0xEC ^ 0x11) data.push(pad);
    // blocks, their error correction, interleaved
    const blocks = [];
    let at = 0;
    for (const [n, d] of groups) for (let k = 0; k < n; k++) { blocks.push(data.slice(at, at + d)); at += d; }
    const ecs = blocks.map(b => rsRemainder(b, ec));
    const out = [];
    for (let i = 0; i < Math.max(...blocks.map(b => b.length)); i++) for (const b of blocks) if (i < b.length) out.push(b[i]);
    for (let i = 0; i < ec; i++) for (const e of ecs) out.push(e[i]);
    return { version: v, words: out };
  }
  throw new Error('too long for a QR code');
}

// ---- the matrix
function build(version, words, mask) {
  const size = version * 4 + 17;
  const m = Array.from({ length: size }, () => new Array(size).fill(false));
  const fn = Array.from({ length: size }, () => new Array(size).fill(false));
  const set = (x, y, dark) => { m[y][x] = dark; fn[y][x] = true; };
  for (let i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }  // timing
  for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]])  // finders + separators
    for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
      const x = cx + dx, y = cy + dy, d = Math.max(Math.abs(dx), Math.abs(dy));
      if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, d !== 2 && d !== 4);
    }
  const al = ALIGN[version], last = al.length - 1;
  al.forEach((cy, i) => al.forEach((cx, j) => {
    if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) return;
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
  }));
  const format = () => {  // level M (00) + mask, BCH(15,5), XOR 0x5412
    const data = mask;
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const bits = ((data << 10) | rem) ^ 0x5412, bit = i => ((bits >>> i) & 1) === 1;
    for (let i = 0; i <= 5; i++) set(8, i, bit(i));
    set(8, 7, bit(6)); set(8, 8, bit(7)); set(7, 8, bit(8));
    for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i));
    for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(i));
    for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(i));
    set(8, size - 8, true);  // the dark module
  };
  format();
  if (version >= 7) {  // version information, BCH(18,6)
    let rem = version;
    for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1F25);
    const bits = (version << 12) | rem;
    for (let i = 0; i < 18; i++) {
      const b = ((bits >>> i) & 1) === 1, a = size - 11 + (i % 3), c = Math.floor(i / 3);
      set(a, c, b); set(c, a, b);
    }
  }
  // data, in the two-column zigzag from the bottom right
  let i = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < size; vert++) for (let j = 0; j < 2; j++) {
      const x = right - j, up = ((right + 1) & 2) === 0, y = up ? size - 1 - vert : vert;
      if (!fn[y][x] && i < words.length * 8) { m[y][x] = ((words[i >>> 3] >>> (7 - (i & 7))) & 1) === 1; i++; }
    }
  }
  const flip = [(x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, x => x % 3 === 0, (x, y) => (x + y) % 3 === 0,
    (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0, (x, y) => (x * y) % 2 + (x * y) % 3 === 0,
    (x, y) => ((x * y) % 2 + (x * y) % 3) % 2 === 0, (x, y) => ((x + y) % 2 + (x * y) % 3) % 2 === 0][mask];
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (!fn[y][x] && flip(x, y)) m[y][x] = !m[y][x];
  return m;
}

// Mask choice: the standard penalty rules (long runs, 2x2 blocks,
// finder-like patterns, dark/light balance); lowest wins.
function penalty(m) {
  const n = m.length;
  let p = 0, dark = 0;
  const lines = [...m, ...m[0].map((_, x) => m.map(r => r[x]))];
  const finder = [true, false, true, true, true, false, true];
  for (const line of lines) {
    let run = 1;
    for (let i = 1; i <= n; i++) {
      if (i < n && line[i] === line[i - 1]) run++;
      else { if (run >= 5) p += run - 2; run = 1; }
    }
    for (let i = 0; i + 7 <= n; i++) {
      if (!finder.every((f, k) => line[i + k] === f)) continue;
      const before = i >= 4 && [1, 2, 3, 4].every(k => !line[i - k]);
      const after = i + 11 <= n && [7, 8, 9, 10].every(k => !line[i + k]);
      if (before || after) p += 40;
    }
  }
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
    if (m[y][x]) dark++;
    if (x < n - 1 && y < n - 1 && m[y][x] === m[y][x + 1] && m[y][x] === m[y + 1][x] && m[y][x] === m[y + 1][x + 1]) p += 3;
  }
  return p + (Math.ceil(Math.abs(dark * 20 - n * n * 10) / (n * n)) - 1) * 10;
}

/** text -> matrix of booleans (true = dark), best mask. */
export function qrMatrix(text, forceMask) {
  const { version, words } = codewords([...new TextEncoder().encode(text)]);
  if (forceMask !== undefined) return build(version, words, forceMask);
  let best = null;
  for (let mask = 0; mask < 8; mask++) {
    const m = build(version, words, mask), s = penalty(m);
    if (!best || s < best.s) best = { m, s };
  }
  return best.m;
}

/** text -> an SVG QR code (black on white, with the quiet zone). */
export function qrSvg(text) {
  const m = qrMatrix(text), n = m.length, q = 4;
  let d = '';
  m.forEach((row, y) => row.forEach((on, x) => { if (on) d += `M${x + q},${y + q}h1v1h-1z`; }));
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${n + 2 * q} ${n + 2 * q}" shape-rendering="crispEdges">` +
    `<rect width="100%" height="100%" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
}
