// Find the room in a scan: floor height, ceiling height (if scanned), and the walls as an
// oriented rectangle. Pure function over world-space points (Y up), so it can be tested alone.
//
// Assumes a "Manhattan" room: walls at right angles, floor level. Real scans have furniture,
// gaps and points seen through windows, so every step prefers dense structure over extremes.

const FLOOR_BAND = 0.35; // look for the floor in the lowest 35% of heights, the ceiling in the top 35%
const WALL_MIN_SHARE = 0.25; // a wall line needs at least this share of the densest line's points

function percentile(sorted, p) {
  if (!sorted.length) return NaN;
  const i = Math.min(sorted.length - 1, Math.max(0, Math.round(p * (sorted.length - 1))));
  return sorted[i];
}

function histogram(values, lo, hi, bin) {
  const n = Math.max(1, Math.ceil((hi - lo) / bin));
  const counts = new Uint32Array(n);
  for (const v of values) {
    if (v < lo || v >= hi) continue;
    counts[Math.floor((v - lo) / bin)]++;
  }
  return counts;
}

function argmaxIn(counts, from, to) {
  let best = -1, bestCount = -1;
  for (let i = Math.max(0, from); i < Math.min(counts.length, to); i++) {
    if (counts[i] > bestCount) { best = i; bestCount = counts[i]; }
  }
  return [best, bestCount];
}

// Sum of squared histogram counts: large when points bunch into thin lines (walls seen edge-on).
function peakiness(us, vs, bin) {
  let score = 0;
  for (const values of [us, vs]) {
    const sorted = Float32Array.from(values).sort();
    const lo = percentile(sorted, 0.01), hi = percentile(sorted, 0.99) + bin;
    for (const c of histogram(values, lo, hi, bin)) score += c * c;
  }
  return score;
}

function rotate(xs, zs, angle) {
  const c = Math.cos(angle), s = Math.sin(angle);
  const us = new Float32Array(xs.length), vs = new Float32Array(xs.length);
  for (let i = 0; i < xs.length; i++) {
    us[i] = c * xs[i] + s * zs[i];
    vs[i] = -s * xs[i] + c * zs[i];
  }
  return [us, vs];
}

// Outermost dense lines along one axis; falls back to the floor's extent where a wall is missing.
function wallsAlong(wallVals, floorVals, bin) {
  const sorted = Float32Array.from(wallVals).sort();
  const lo = percentile(sorted, 0.005) - bin, hi = percentile(sorted, 0.995) + 2 * bin;
  const counts = histogram(wallVals, lo, hi, bin);
  let max = 0;
  for (const c of counts) max = Math.max(max, c);
  const threshold = max * WALL_MIN_SHARE;
  const peaks = [];
  for (let i = 0; i < counts.length; i++) {
    const c = counts[i];
    if (c >= threshold && c >= (counts[i - 1] ?? 0) && c >= (counts[i + 1] ?? 0)) peaks.push(lo + (i + 0.5) * bin);
  }
  const floorSorted = Float32Array.from(floorVals).sort();
  const floorLo = percentile(floorSorted, 0.01), floorHi = percentile(floorSorted, 0.99);
  const mid = (floorLo + floorHi) / 2 || (lo + hi) / 2;
  // A wall must sit in the outer part of the room, not be a sofa back in the middle.
  const span = (floorHi - floorLo) || (hi - lo);
  const lowPeaks = peaks.filter((p) => p < mid - span * 0.2);
  const highPeaks = peaks.filter((p) => p > mid + span * 0.2);
  return {
    min: lowPeaks.length ? lowPeaks[0] : floorLo,
    max: highPeaks.length ? highPeaks[highPeaks.length - 1] : floorHi,
    minFound: lowPeaks.length > 0,
    maxFound: highPeaks.length > 0,
  };
}

// Keep the dense core: scans often include a far "background" shell of splats (hundreds of
// metres out) or floaters, enough to skew every statistic. Per axis, keep points within half
// the 5-95% spread beyond it.
function trimToCore(pts) {
  const n = Math.floor(pts.length / 3);
  const bounds = [];
  for (let a = 0; a < 3; a++) {
    const vals = new Float32Array(n);
    for (let i = 0; i < n; i++) vals[i] = pts[3 * i + a];
    vals.sort();
    const p5 = percentile(vals, 0.05), p95 = percentile(vals, 0.95);
    const pad = (p95 - p5) * 0.5;
    bounds.push([p5 - pad, p95 + pad]);
  }
  const out = [];
  for (let i = 0; i < n; i++) {
    const x = pts[3 * i], y = pts[3 * i + 1], z = pts[3 * i + 2];
    if (x >= bounds[0][0] && x <= bounds[0][1] && y >= bounds[1][0] && y <= bounds[1][1] &&
        z >= bounds[2][0] && z <= bounds[2][1]) out.push(x, y, z);
  }
  return new Float32Array(out);
}

/**
 * @param {Float32Array} pts  x,y,z triples in world space, Y up (metres if the scan is metric)
 * @returns room description, or null if there's too little to work with
 */
export function detectRoom(allPts) {
  const pts = trimToCore(allPts);
  const n = Math.floor(pts.length / 3);
  if (n < 1000) return null;

  // --- floor and ceiling from the height histogram ---
  const ys = new Float32Array(n);
  for (let i = 0; i < n; i++) ys[i] = pts[3 * i + 1];
  const ySorted = Float32Array.from(ys).sort();
  const yLo = percentile(ySorted, 0.005), yHi = percentile(ySorted, 0.995);
  const range = yHi - yLo;
  if (!(range > 0)) return null;
  const ybin = Math.max(range / 400, 0.01);
  const raw = histogram(ys, yLo, yHi + ybin, ybin);
  // Smooth over ~5 bins so single noisy bins don't count as layers.
  const yCounts = new Float32Array(raw.length);
  for (let i = 0; i < raw.length; i++) {
    let sum = 0, k = 0;
    for (let j = i - 2; j <= i + 2; j++) if (j >= 0 && j < raw.length) { sum += raw[j]; k++; }
    yCounts[i] = sum / k;
  }
  let maxCount = 0;
  for (const c of yCounts) maxCount = Math.max(maxCount, c);

  // A horizontal layer (floor, ceiling) is a local peak that clearly stands out from the space
  // just beside it on the room side. The floor is the LOWEST such layer and the ceiling the
  // HIGHEST: table and bed tops can be much denser than a dark floor, so "densest" is wrong.
  const near = Math.max(1, Math.round(0.10 / ybin)), far = Math.max(near + 1, Math.round(0.30 / ybin));
  const isLayer = (i, towardRoom) => {
    const c = yCounts[i];
    if (c < maxCount * 0.12 || c < (yCounts[i - 1] ?? 0) || c < (yCounts[i + 1] ?? 0)) return false;
    let sum = 0, k = 0;
    for (let d = near; d <= far; d++) {
      const j = i + towardRoom * d;
      if (j >= 0 && j < yCounts.length) { sum += yCounts[j]; k++; }
    }
    return k > 0 && c >= 2 * (sum / k);
  };
  const half = Math.floor(yCounts.length / 2);
  let floorBin = -1;
  for (let i = 0; i < half && floorBin < 0; i++) if (isLayer(i, +1)) floorBin = i;
  if (floorBin < 0) floorBin = argmaxIn(yCounts, 0, Math.ceil(yCounts.length * FLOOR_BAND))[0];
  let ceilBin = -1;
  for (let i = yCounts.length - 1; i > half && ceilBin < 0; i--) if (isLayer(i, -1)) ceilBin = i;
  const floorY = yLo + (floorBin + 0.5) * ybin;
  const hasCeiling = ceilBin > floorBin;
  const ceilY = hasCeiling ? yLo + (ceilBin + 0.5) * ybin : null;
  const top = ceilY ?? yHi;

  // Which way is up? Furniture fills the metre above a floor; the metre below a ceiling is
  // mostly empty. If the "ceiling" side is much busier, the scan is upside down.
  let nearFloor = 0, nearCeiling = 0;
  if (ceilY !== null) {
    const band = Math.min(1.1, (ceilY - floorY) * 0.45);
    for (let i = 0; i < n; i++) {
      const y = pts[3 * i + 1];
      if (y > floorY + 0.15 && y < floorY + band) nearFloor++;
      else if (y < ceilY - 0.15 && y > ceilY - band) nearCeiling++;
    }
  }
  const upsideDown = ceilY !== null && nearCeiling > 1.3 * nearFloor;

  // --- split into floor points and wall-band points ---
  // (h is floor to ceiling, or to the top of the scan when there's no ceiling)
  const h = top - floorY;
  const floorX = [], floorZ = [], wallX = [], wallZ = [];
  for (let i = 0; i < n; i++) {
    const x = pts[3 * i], y = pts[3 * i + 1], z = pts[3 * i + 2];
    if (Math.abs(y - floorY) < 2 * ybin) { floorX.push(x); floorZ.push(z); }
    // Walls are cleanest above the furniture: use the upper half of the room.
    else if (y > floorY + 0.5 * h && y < floorY + 0.9 * h) { wallX.push(x); wallZ.push(z); }
  }
  if (wallX.length < 200) return null;

  // Subsample the orientation search: 60k points is plenty to see the walls.
  const step = Math.max(1, Math.floor(wallX.length / 60000));
  const sx = [], sz = [];
  for (let i = 0; i < wallX.length; i += step) { sx.push(wallX[i]); sz.push(wallZ[i]); }

  // --- wall orientation: the rotation that makes wall points bunch into the thinnest lines ---
  const span = Math.max(range, 1);
  const bin = Math.max(span / 250, 0.02);
  const deg = Math.PI / 180;
  let bestAngle = 0, bestScore = -1;
  for (let a = 0; a < 90; a += 1) {
    const [us, vs] = rotate(sx, sz, a * deg);
    const score = peakiness(us, vs, bin);
    if (score > bestScore) { bestScore = score; bestAngle = a; }
  }
  for (let a = bestAngle - 1; a <= bestAngle + 1; a += 0.1) {
    const [us, vs] = rotate(sx, sz, a * deg);
    const score = peakiness(us, vs, bin);
    if (score > bestScore) { bestScore = score; bestAngle = a; }
  }
  const angle = bestAngle * deg;

  // --- walls along each rotated axis ---
  const [wu, wv] = rotate(wallX, wallZ, angle);
  const [fu, fv] = rotate(floorX, floorZ, angle);
  const U = wallsAlong(wu, fu, bin);
  const V = wallsAlong(wv, fv, bin);

  // corners back in world x/z (rotate by -angle)
  const c = Math.cos(angle), s = Math.sin(angle);
  const toWorld = (u, v) => ({ x: c * u - s * v, z: s * u + c * v });
  const corners = [toWorld(U.min, V.min), toWorld(U.max, V.min), toWorld(U.max, V.max), toWorld(U.min, V.max)];

  return {
    angle, corners, floorY, ceilY, upsideDown,
    width: U.max - U.min, depth: V.max - V.min, height: ceilY === null ? null : ceilY - floorY,
    wallsFound: [U.minFound, U.maxFound, V.minFound, V.maxFound].filter(Boolean).length,
    points: n,
  };
}
