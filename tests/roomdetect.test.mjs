// node tests/roomdetect.test.mjs : checks detectRoom against synthetic rooms with known answers.
import { detectRoom } from "../offcentre/web/roomdetect.js";

let seed = 7;
const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const gauss = () => Math.sqrt(-2 * Math.log(rand() + 1e-12)) * Math.cos(2 * Math.PI * rand());

function room(spec) {
  const { w, d, h, angleDeg, walls = [1, 1, 1, 1], ceiling = true, clutter = true, outside = 0 } = spec;
  const pts = [];
  const a = angleDeg * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
  const put = (u, y, v) => { pts.push(c * u - s * v + 1.3, y - 0.4, s * u + c * v - 2.1); }; // offset origin too
  const noise = () => gauss() * 0.01;
  for (let i = 0; i < 60000; i++) put(rand() * w, noise(), rand() * d);                    // floor
  if (ceiling) for (let i = 0; i < 40000; i++) put(rand() * w, h + noise(), rand() * d);   // ceiling
  const wallN = 25000;
  if (walls[0]) for (let i = 0; i < wallN; i++) put(noise(), rand() * h, rand() * d);        // u = 0
  if (walls[1]) for (let i = 0; i < wallN; i++) put(w + noise(), rand() * h, rand() * d);    // u = w
  if (walls[2]) for (let i = 0; i < wallN; i++) put(rand() * w, rand() * h, noise());        // v = 0
  if (walls[3]) for (let i = 0; i < wallN; i++) put(rand() * w, rand() * h, d + noise());    // v = d
  if (clutter) {
    for (let i = 0; i < 15000; i++) put(1 + rand() * 2, rand() * 0.9, 1.5 + rand() * 0.9); // sofa
    for (let i = 0; i < 6000; i++) put(w * 0.5 + rand() * 1.2, rand() * 0.75, 0.3 + rand() * 0.5); // cabinet
  }
  for (let i = 0; i < outside; i++) put(w + 0.5 + rand() * 4, rand() * h, rand() * d);       // seen through a window
  for (let i = 0; i < (spec.shell || 0); i++) {                                               // far background shell
    const th = rand() * 2 * Math.PI, ph = Math.acos(2 * rand() - 1), R = 130 + rand() * 10;
    pts.push(R * Math.sin(ph) * Math.cos(th), R * Math.cos(ph), R * Math.sin(ph) * Math.sin(th));
  }
  return new Float32Array(pts);
}

function angleErr(got, want) {
  let e = Math.abs(((got * 180 / Math.PI) - want) % 90);
  return Math.min(e, 90 - e);
}

let failed = 0;
function check(name, spec, tol = 0.06) {
  const r = detectRoom(room(spec));
  const dims = [r.width, r.depth].sort((x, y) => x - y), want = [spec.w, spec.d].sort((x, y) => x - y);
  const ok = r && angleErr(r.angle, spec.angleDeg) < 1 &&
    Math.abs(dims[0] - want[0]) < tol && Math.abs(dims[1] - want[1]) < tol &&
    Math.abs(r.floorY - -0.4) < 0.03 &&
    (spec.ceiling === false ? r.height === null : Math.abs(r.height - spec.h) < 0.05);
  if (!ok) failed++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}: ${r.width.toFixed(2)} x ${r.depth.toFixed(2)} x ${r.height?.toFixed(2) ?? "-"} m, ` +
    `angle ${(r.angle * 180 / Math.PI).toFixed(1)}, walls found ${r.wallsFound}/4, floor ${r.floorY.toFixed(3)}`);
}

check("axis-aligned", { w: 6.5, d: 4.6, h: 2.5, angleDeg: 0 });
check("rotated 17 degrees", { w: 6.5, d: 4.6, h: 2.5, angleDeg: 17 });
check("rotated 62 degrees, square-ish", { w: 4.2, d: 3.9, h: 2.4, angleDeg: 62 });
check("one wall missing, no ceiling", { w: 5.0, d: 4.0, h: 2.6, angleDeg: 33, walls: [1, 0, 1, 1], ceiling: false }, 0.12);
check("points seen through a window", { w: 5.5, d: 4.2, h: 2.5, angleDeg: 8, outside: 8000 });
check("far background shell (as in real phone scans)", { w: 6.1, d: 4.4, h: 2.6, angleDeg: 21, shell: 15000 });
process.exit(failed ? 1 : 0);
