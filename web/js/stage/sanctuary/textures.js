/* Procedural surfaces for the sanctuary (SPEC §6.2) — every map in the room is
 * drawn into a <canvas> at boot and uploaded once, so the set ships as readable
 * code and git never holds a binary scene asset.
 *
 * The palette is the page's (SPEC §6.3, web/sanctuary.css): magenta, cyan and
 * amber over near-black. Her room and her chrome are the same brand.
 */
import { CanvasTexture, ClampToEdgeWrapping, RepeatWrapping, SRGBColorSpace } from 'three';

export const PALETTE = {
  magenta: '#ff2bd6',
  cyan: '#2bfff0',
  amber: '#f5b462',
  violet: '#9b5cff',
  void: '#050507',
};

// Anisotropy needs the renderer's capabilities, which the scene has and this
// module does not — SanctuaryScene calls this before drawing anything.
let ANISO = 1;
export function configureTextures(renderer) {
  ANISO = renderer.capabilities.getMaxAnisotropy();
}

const R = Math.random;

export function mkCanvas(w, h = w) {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  return c;
}

export function toTex(canvas, { repeat = [1, 1], srgb = true, clamp = false } = {}) {
  const t = new CanvasTexture(canvas);
  t.wrapS = t.wrapT = clamp ? ClampToEdgeWrapping : RepeatWrapping;
  t.repeat.set(repeat[0], repeat[1]);
  t.anisotropy = ANISO;
  if (srgb) t.colorSpace = SRGBColorSpace;
  return t;
}

/** Sobel a greyscale height canvas into a tangent-space normal map — cheaper to
 *  author than hand-painted normals and it keeps the seams/rivets legible under
 *  the room's grazing light. */
export function heightToNormal(srcCanvas, strength = 2.2) {
  const size = srcCanvas.width;
  const data = srcCanvas.getContext('2d').getImageData(0, 0, size, size).data;
  const c = mkCanvas(size);
  const ctx = c.getContext('2d');
  const out = ctx.createImageData(size, size);
  const h = (x, y) => data[((((y + size) % size) * size) + ((x + size) % size)) * 4] / 255;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = (h(x + 1, y) - h(x - 1, y)) * strength;
      const dy = (h(x, y + 1) - h(x, y - 1)) * strength;
      const inv = 1 / Math.hypot(dx, dy, 1);
      const i = (y * size + x) * 4;
      out.data[i] = (-dx * inv * 0.5 + 0.5) * 255;
      out.data[i + 1] = (dy * inv * 0.5 + 0.5) * 255;
      out.data[i + 2] = (inv * 0.5 + 0.5) * 255;
      out.data[i + 3] = 255;
    }
  }
  ctx.putImageData(out, 0, 0);
  const t = new CanvasTexture(c);
  t.wrapS = t.wrapT = RepeatWrapping;
  t.anisotropy = ANISO;
  return t;
}

/** The floor: dark deck plates, bevelled and riveted, worn by whoever lived here
 *  before her. Wet-looking under the window (the reflector does the rest). */
export function createFloorMaps() {
  const S = 512, plates = 4, ps = S / plates;
  const col = mkCanvas(S), hgt = mkCanvas(S), rgh = mkCanvas(S);
  const c = col.getContext('2d'), h = hgt.getContext('2d'), r = rgh.getContext('2d');
  c.fillStyle = '#0b0b11'; c.fillRect(0, 0, S, S);
  h.fillStyle = '#202020'; h.fillRect(0, 0, S, S);
  r.fillStyle = '#b4b4b4'; r.fillRect(0, 0, S, S);

  for (let i = 0; i < plates; i++) for (let j = 0; j < plates; j++) {
    const x = i * ps, y = j * ps;
    const v = 15 + R() * 13;
    c.fillStyle = `rgb(${v},${v + 1},${v + 8})`;
    c.fillRect(x + 3, y + 3, ps - 6, ps - 6);
    const hv = Math.floor(175 + R() * 25);
    h.fillStyle = `rgb(${hv},${hv},${hv})`;
    h.fillRect(x + 3, y + 3, ps - 6, ps - 6);
    c.fillStyle = 'rgba(120,130,160,0.10)';       // bevel catch-light
    c.fillRect(x + 3, y + 3, ps - 6, 2);
    c.fillRect(x + 3, y + 3, 2, ps - 6);
    c.fillStyle = 'rgba(0,0,0,0.35)';
    c.fillRect(x + 3, y + ps - 5, ps - 6, 2);
    c.fillRect(x + ps - 5, y + 3, 2, ps - 6);
    for (const [rx, ry] of [[12, 12], [ps - 12, 12], [12, ps - 12], [ps - 12, ps - 12]]) {
      c.fillStyle = '#34343f';
      c.beginPath(); c.arc(x + rx, y + ry, 3, 0, 7); c.fill();
      c.fillStyle = 'rgba(160,170,200,0.45)';
      c.beginPath(); c.arc(x + rx - 1, y + ry - 1, 1.2, 0, 7); c.fill();
      h.fillStyle = '#f0f0f0';
      h.beginPath(); h.arc(x + rx, y + ry, 3, 0, 7); h.fill();
    }
    r.fillStyle = `rgba(${120 + R() * 60},${120 + R() * 60},${120 + R() * 60},0.55)`;
    r.fillRect(x + 3, y + 3, ps - 6, ps - 6);
    if (R() < 0.3) {                               // an old stain, left by someone
      const sx = x + R() * ps, sy = y + R() * ps, sr = 12 + R() * 30;
      const g = c.createRadialGradient(sx, sy, 2, sx, sy, sr);
      g.addColorStop(0, 'rgba(0,0,0,0.5)'); g.addColorStop(1, 'rgba(0,0,0,0)');
      c.fillStyle = g; c.beginPath(); c.arc(sx, sy, sr, 0, 7); c.fill();
      const rg = r.createRadialGradient(sx, sy, 2, sx, sy, sr);
      rg.addColorStop(0, 'rgba(30,30,30,0.8)'); rg.addColorStop(1, 'rgba(30,30,30,0)');
      r.fillStyle = rg; r.beginPath(); r.arc(sx, sy, sr, 0, 7); r.fill();
    }
  }
  for (let i = 0; i < 60; i++) {                   // scratches
    const x = R() * S, y = R() * S, a = R() * Math.PI, l = 10 + R() * 60;
    c.strokeStyle = `rgba(140,150,180,${0.04 + R() * 0.08})`;
    c.lineWidth = 0.8;
    c.beginPath(); c.moveTo(x, y); c.lineTo(x + Math.cos(a) * l, y + Math.sin(a) * l); c.stroke();
  }
  for (let i = 0; i < 4200; i++) {                 // grit
    const v = R() * 24;
    c.fillStyle = `rgba(${v + 8},${v + 8},${v + 15},0.12)`;
    c.fillRect(R() * S, R() * S, 1.4, 1.4);
  }
  return {
    map: toTex(col, { repeat: [2.4, 3.2] }),
    normalMap: (() => { const t = heightToNormal(hgt, 2.4); t.repeat.set(2.4, 3.2); return t; })(),
    roughnessMap: toTex(rgh, { repeat: [2.4, 3.2], srgb: false }),
  };
}

/** The walls: prefab structural panels with vents and forty years of grime —
 *  a unit in a stacked block, not a designed interior. */
export function createWallMaps() {
  const S = 512;
  const col = mkCanvas(S), hgt = mkCanvas(S), rgh = mkCanvas(S);
  const c = col.getContext('2d'), h = hgt.getContext('2d'), r = rgh.getContext('2d');
  c.fillStyle = '#13131b'; c.fillRect(0, 0, S, S);
  h.fillStyle = '#7a7a7a'; h.fillRect(0, 0, S, S);
  r.fillStyle = '#b0b0b0'; r.fillRect(0, 0, S, S);

  const rows = 3, cols = 4;
  for (let i = 0; i < cols; i++) for (let j = 0; j < rows; j++) {
    const w = S / cols, hh = S / rows;
    const x = i * w, y = j * hh;
    const v = 19 + R() * 9;
    c.fillStyle = `rgb(${v},${v},${v + 8})`;
    c.fillRect(x + 4, y + 4, w - 8, hh - 8);
    h.fillStyle = '#9a9a9a'; h.fillRect(x + 4, y + 4, w - 8, hh - 8);
    const rv = 120 + R() * 65;
    r.fillStyle = `rgb(${rv},${rv},${rv})`;
    r.fillRect(x + 4, y + 4, w - 8, hh - 8);
    h.fillStyle = '#2a2a2a';
    h.fillRect(x, y, w, 3); h.fillRect(x, y, 3, hh);
    c.fillStyle = 'rgba(0,0,0,0.5)';
    c.fillRect(x, y, w, 3); c.fillRect(x, y, 3, hh);
    c.fillStyle = 'rgba(130,140,175,0.07)';
    c.fillRect(x + 4, y + 4, w - 8, 2);
    for (const [rx, ry] of [[10, 10], [w - 10, 10], [10, hh - 10], [w - 10, hh - 10]]) {
      c.fillStyle = '#33333e';
      c.beginPath(); c.arc(x + rx, y + ry, 2.5, 0, 7); c.fill();
      h.fillStyle = '#e8e8e8';
      h.beginPath(); h.arc(x + rx, y + ry, 2.5, 0, 7); h.fill();
    }
    if (R() < 0.28) {                              // vent grille
      const vx = x + w * 0.25, vy = y + hh * 0.35, vw = w * 0.5, vh = hh * 0.3;
      c.fillStyle = '#08080e'; c.fillRect(vx, vy, vw, vh);
      for (let k = 0; k < 6; k++) {
        c.fillStyle = '#282833';
        c.fillRect(vx + 3, vy + 3 + k * (vh - 6) / 6, vw - 6, 2.5);
        h.fillStyle = k % 2 ? '#c0c0c0' : '#404040';
        h.fillRect(vx + 3, vy + 3 + k * (vh - 6) / 6, vw - 6, 2.5);
      }
    }
  }
  for (let i = 0; i < 26; i++) {                   // water running down, for years
    const x = R() * S, y0 = R() * S * 0.4, len = 40 + R() * 140;
    const g = c.createLinearGradient(0, y0, 0, y0 + len);
    g.addColorStop(0, 'rgba(0,0,0,0.28)'); g.addColorStop(1, 'rgba(0,0,0,0)');
    c.fillStyle = g;
    c.fillRect(x, y0, 2 + R() * 5, len);
    r.fillStyle = 'rgba(30,30,30,0.28)';
    r.fillRect(x, y0, 2 + R() * 5, len);
  }
  for (let i = 0; i < 3600; i++) {
    const v = R() * 18;
    c.fillStyle = `rgba(${v + 9},${v + 9},${v + 16},0.12)`;
    c.fillRect(R() * S, R() * S, 1.2, 1.2);
    if (i < 1500) {
      const rv = 100 + R() * 110;
      r.fillStyle = `rgba(${rv},${rv},${rv},0.14)`;
      r.fillRect(R() * S, R() * S, 1.5, 1.5);
    }
  }
  return {
    map: toTex(col, { repeat: [2, 1] }),
    normalMap: (() => { const t = heightToNormal(hgt, 1.8); t.repeat.set(2, 1); return t; })(),
    roughnessMap: toTex(rgh, { repeat: [2, 1], srgb: false }),
  };
}

/** Brushed metal for the frames, the desk, the shelving. */
export function createMetalTex() {
  const S = 256, cv = mkCanvas(S), c = cv.getContext('2d');
  c.fillStyle = '#21212b'; c.fillRect(0, 0, S, S);
  for (let i = 0; i < 220; i++) {
    const y = R() * S;
    c.strokeStyle = `rgba(110,115,140,${0.03 + R() * 0.08})`;
    c.lineWidth = 0.7;
    c.beginPath(); c.moveTo(0, y); c.lineTo(S, y + (R() - 0.5) * 5); c.stroke();
  }
  return toTex(cv, { repeat: [2, 2] });
}

/** The woven cloth of the window-seat cushion and the rug — the one soft thing
 *  in a room made of panels. Threadbare on purpose: it has been used. */
export function createWeaveTex() {
  const S = 512, cv = mkCanvas(S), c = cv.getContext('2d');
  c.fillStyle = '#1a1522'; c.fillRect(0, 0, S, S);
  for (let i = 0; i < 9000; i++) {
    const v = R() * 24;
    c.fillStyle = `rgba(${34 + v},${24 + v},${44 + v},0.35)`;
    c.fillRect(R() * S, R() * S, 2, 1.2);
  }
  c.strokeStyle = 'rgba(255,43,214,0.16)'; c.lineWidth = 5;
  c.strokeRect(28, 28, S - 56, S - 56);
  c.strokeStyle = 'rgba(43,255,240,0.12)'; c.lineWidth = 3;
  c.strokeRect(52, 52, S - 104, S - 104);
  c.strokeStyle = 'rgba(155,92,255,0.12)';
  for (let i = 0; i < 5; i++) {
    c.beginPath();
    c.moveTo(80 + i * 90, 80); c.lineTo(160 + i * 90, S - 80); c.stroke();
  }
  for (let i = 0; i < 14; i++) {                   // worn patches
    const x = R() * S, y = R() * S, rr = 15 + R() * 45;
    const g = c.createRadialGradient(x, y, 2, x, y, rr);
    g.addColorStop(0, 'rgba(10,8,14,0.5)'); g.addColorStop(1, 'rgba(0,0,0,0)');
    c.fillStyle = g; c.beginPath(); c.arc(x, y, rr, 0, 7); c.fill();
  }
  return toTex(cv);
}

/** A printed sheet taped to a wall — a Lab field note, a warning, a handbill. */
export function createPosterTex(lines, accent, bg) {
  const cv = mkCanvas(256, 384), c = cv.getContext('2d');
  c.fillStyle = bg; c.fillRect(0, 0, 256, 384);
  for (let i = 0; i < 10; i++) {                   // press misregistration
    c.fillStyle = `rgba(${R() * 255 | 0},${R() * 255 | 0},${R() * 255 | 0},0.07)`;
    c.fillRect(0, R() * 384, 256, 3 + R() * 10);
  }
  c.fillStyle = accent;
  c.fillRect(0, 30, 256, 6);
  c.fillRect(0, 348, 256, 6);
  c.textAlign = 'center';
  lines.forEach((ln, i) => {
    c.fillStyle = i === 0 ? accent : '#e8e6f0';
    c.font = i === 0 ? 'bold 42px monospace' : 'bold 22px monospace';
    c.fillText(ln, 128, 140 + i * 52);
  });
  c.fillStyle = 'rgba(0,0,0,0.18)';
  for (let y = 0; y < 384; y += 4) c.fillRect(0, y, 256, 1);
  for (let i = 0; i < 300; i++) {
    c.fillStyle = `rgba(0,0,0,${R() * 0.25})`;
    c.fillRect(R() * 256, R() * 384, 2, 2);
  }
  return toTex(cv);
}

/** Glow-on-transparent lettering for the small neon pieces she has kept. */
export function createNeonSignTex(text, color, vertical = false) {
  const cv = vertical ? mkCanvas(160, 512) : mkCanvas(512, 192);
  const c = cv.getContext('2d');
  c.clearRect(0, 0, cv.width, cv.height);
  c.fillStyle = color;
  c.shadowColor = color;
  c.shadowBlur = 26;
  c.textAlign = 'center';
  c.textBaseline = 'middle';
  if (vertical) {
    c.font = 'bold 96px monospace';
    [...text].forEach((ch, i) => {
      c.fillText(ch, 80, 90 + i * 110);
      c.fillText(ch, 80, 90 + i * 110);           // twice: a hotter core
    });
  } else {
    c.font = 'bold 84px monospace';
    c.fillText(text, 256, 100);
    c.fillText(text, 256, 100);
  }
  return toTex(cv);
}

/** Her keyboard: backlit keycaps in the page's three accents. */
export function createKeycapTex() {
  const cv = mkCanvas(256, 96), c = cv.getContext('2d');
  c.fillStyle = '#0a0a10'; c.fillRect(0, 0, 256, 96);
  const cols = [PALETTE.magenta, PALETTE.cyan, PALETTE.amber, PALETTE.violet];
  for (let row = 0; row < 4; row++) {
    for (let k = 0; k < 14; k++) {
      const x = 6 + k * 17.6, y = 8 + row * 21;
      c.fillStyle = '#16161f';
      c.fillRect(x, y, 14, 16);
      c.fillStyle = cols[Math.floor(R() * cols.length)];
      c.globalAlpha = 0.25 + R() * 0.45;
      c.fillRect(x + 3, y + 3, 8, 2.5);
      c.globalAlpha = 1;
    }
  }
  return toTex(cv);
}
