// 3D scan view: render a Gaussian splat of the room (from Scaniverse, Polycam, Luma...) with Spark,
// let the user click the two HomePods and their head position on it, and copy the measured
// layout to the plan.
//
// three.js and Spark are only downloaded when this view is first opened. The scan file never
// leaves the browser; placed markers are remembered per file in localStorage.

const $ = (id) => document.getElementById(id);
import { detectRoom } from "./roomdetect.js";

const MARKERS = {
  left: { name: "Left HomePod", color: 0x5b8ff0 },
  right: { name: "Right HomePod", color: 0xe39045 },
  seat: { name: "Your head", color: 0x5cc07f },
};
const ORDER = ["left", "right", "seat"];

let THREE, OrbitControls, TransformControls, SplatMesh, SparkRenderer;
let renderer, scene, camera, controls;
let gizmo = null; // X/Y/Z move handles on the selected marker
let splat = null; // current SplatMesh
let fileKey = null; // localStorage key for the current file
let picks = {}; // marker -> THREE.Vector3 in the splat's local frame
let markerObjs = {};
let lines = null;
let current = "left";
let visible = false;
let moveSpeed = 1; // metres per second for WASD, set from the scan's size when it loads
const held = new Set(); // movement keys currently down
let lastFrame = 0;
let room = null; // detected room (world space), see roomdetect.js
let orientationChecked = false; // auto-flip at most once per loaded scan
let roomLines = null;

// ---------- view switching ----------

function showView(scan) {
  $("plan-view").hidden = scan;
  $("scan-view").hidden = !scan;
  $("view-plan").setAttribute("aria-pressed", String(!scan));
  $("view-scan").setAttribute("aria-pressed", String(scan));
  visible = scan;
  if (scan) init().then(() => { resize(); renderer?.setAnimationLoop(frame); });
  else renderer?.setAnimationLoop(null);
}
$("view-plan").onclick = () => showView(false);
$("view-scan").onclick = () => showView(true);

function status(text) {
  const el = $("scan-status");
  el.hidden = !text;
  el.textContent = text || "";
}

// ---------- three.js setup (lazy) ----------

let initPromise = null;
function init() {
  initPromise ??= (async () => {
    status("Loading 3D viewer…");
    try {
      THREE = await import("three");
      ({ OrbitControls } = await import("three/addons/controls/OrbitControls.js"));
      ({ TransformControls } = await import("three/addons/controls/TransformControls.js"));
      ({ SplatMesh, SparkRenderer } = await import("@sparkjsdev/spark"));
    } catch (e) {
      status("Couldn't load the 3D viewer (needs internet the first time): " + e.message);
      throw e;
    }
    const stage = $("scan-stage");
    try {
      renderer = new THREE.WebGLRenderer({ antialias: false });
    } catch (e) {
      status("WebGL isn't available in this browser.");
      throw e;
    }
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    stage.prepend(renderer.domElement);
    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(60, 1, 0.01, 500);
    camera.position.set(0, 1.5, 3);
    scene.add(new SparkRenderer({ renderer }));
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.enableZoom = false; // the wheel moves the camera instead, see onWheel
    renderer.domElement.addEventListener("wheel", onWheel, { passive: false });
    initGizmo();
    new ResizeObserver(resize).observe(stage);
    wirePicking();
    status("");
  })();
  return initPromise;
}

function resize() {
  if (!renderer) return;
  const stage = $("scan-stage");
  const w = stage.clientWidth, h = stage.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

function frame(now) {
  const dt = lastFrame ? Math.min((now - lastFrame) / 1000, 0.1) : 0;
  lastFrame = now;
  walk(dt);
  controls.update();
  renderer.render(scene, camera);
}

// ---------- loading a scan ----------

async function load(file) {
  if (!file) return;
  if (!/\.(ply|spz|splat|ksplat)$/i.test(file.name)) {
    status(`${file.name} isn't a splat file (.ply, .spz, .splat, .ksplat).`);
    return;
  }
  showView(true);
  await init();
  status(`Reading ${file.name} (${(file.size / 1e6).toFixed(0)} MB)…`);
  try {
    const bytes = new Uint8Array(await file.arrayBuffer()); // Spark wants a Uint8Array, not a raw buffer
    if (splat) { scene.remove(splat); splat.dispose?.(); }
    splat = new SplatMesh({ fileBytes: bytes, fileName: file.name });
    applyFlip();
    scene.add(splat);
    await splat.initialized;
  } catch (e) {
    status(`Couldn't load ${file.name}: ${e.message}`);
    return;
  }
  $("scan-drop").hidden = true;
  orientationChecked = false;
  fileKey = `offcentre-scan:${file.name}:${file.size}`;
  restore();
  frameCamera();
  findRoom();
  status(`${file.name} · click to place the ${MARKERS[current].name.toLowerCase()}`);
}

function frameCamera() {
  splat.updateMatrixWorld(true);
  const box = splat.getBoundingBox(true).applyMatrix4(splat.matrixWorld);
  const centre = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3()).length();
  controls.target.copy(centre);
  // From above, tilted slightly: we don't know which side the scan was taken from, but looking
  // down into the room always shows the layout. (Zoom in past the ceiling if the scan has one.)
  camera.position.copy(centre).add(new THREE.Vector3(0, size * 0.75, size * 0.25));
  camera.near = Math.max(size / 5000, 0.005);
  camera.far = size * 20;
  moveSpeed = Math.max(size * 0.25, 0.5);
  camera.updateProjectionMatrix();
  controls.update();
}

// Most 3DGS exports are Y-down (OpenCV); a 180° turn about X stands them upright.
function applyFlip() {
  if (!splat) return;
  if ($("scan-flip").checked) splat.quaternion.set(1, 0, 0, 0);
  else splat.quaternion.set(0, 0, 0, 1);
  splat.updateMatrixWorld(true);
}
$("scan-flip").addEventListener("change", () => {
  orientationChecked = true; // the user's choice overrides auto-detection
  applyFlip();
  save();
  if (splat) { frameCamera(); update(); findRoom(); } // "up" changed, so the room must be re-found
});

// ---------- room detection ----------

// Collect splat centres in world space (subsampled on huge scans) and find floor, walls and
// ceiling. Then outline the room and frame the camera to it rather than to stray splats.
function findRoom() {
  status("Finding the room…");
  setTimeout(() => {
    splat.updateMatrixWorld(true);
    const e = splat.matrixWorld.elements;
    const total = splat.packedSplats?.numSplats ?? 0;
    const stride = Math.max(1, Math.ceil(total / 400000));
    const pts = [];
    splat.forEachSplat((i, c, _scales, _q, opacity) => {
      if (i % stride || opacity < 0.3) return; // faint splats are mostly floaters
      pts.push(e[0] * c.x + e[4] * c.y + e[8] * c.z + e[12],
               e[1] * c.x + e[5] * c.y + e[9] * c.z + e[13],
               e[2] * c.x + e[6] * c.y + e[10] * c.z + e[14]);
    });
    room = detectRoom(new Float32Array(pts));
    // Exports differ on which way is up; the room itself tells us. Turn it over once if needed.
    if (room?.upsideDown && !orientationChecked) {
      orientationChecked = true;
      $("scan-flip").checked = !$("scan-flip").checked;
      applyFlip();
      save();
      frameCamera();
      update();
      status("The scan was upside down, so it's been turned over. Finding the room…");
      findRoom();
      return;
    }
    orientationChecked = true;
    drawRoom();
    if (room) frameToRoom();
    measure();
    const next = ORDER.find((k) => !picks[k]);
    status(room ? (next ? `Room found. Click to place the ${MARKERS[next].name.toLowerCase()}` : "Room found.")
                : "Couldn't make out the room's walls; you can still place the markers.");
  }, 30);
}

function drawRoom() {
  if (roomLines) { scene.remove(roomLines); roomLines.geometry.dispose(); roomLines = null; }
  if (!room) return;
  const top = room.ceilY ?? room.floorY + 2.4;
  const pts = [];
  const cs = room.corners;
  for (let i = 0; i < 4; i++) {
    const a = cs[i], b = cs[(i + 1) % 4];
    pts.push(new THREE.Vector3(a.x, room.floorY, a.z), new THREE.Vector3(b.x, room.floorY, b.z)); // floor edge
    pts.push(new THREE.Vector3(a.x, top, a.z), new THREE.Vector3(b.x, top, b.z));                 // top edge
    pts.push(new THREE.Vector3(a.x, room.floorY, a.z), new THREE.Vector3(a.x, top, a.z));         // corner post
  }
  roomLines = new THREE.LineSegments(
    new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineBasicMaterial({ color: 0x9fd0ff, transparent: true, opacity: 0.55, depthTest: false }),
  );
  roomLines.renderOrder = 8;
  scene.add(roomLines);
}

// Stand a little back from the middle of the room at eye height, looking along its longer side
// and slightly down. Phone scans are captured from the middle, so that's where they're sharpest
// (corners are the worst-covered part), and inside the room a scanned ceiling can't block the
// view or catch clicks. Walking speed and clipping planes are scaled to the room too.
function frameToRoom() {
  const [c0, c1, , c3] = room.corners;
  const cx = room.corners.reduce((a, c) => a + c.x, 0) / 4;
  const cz = room.corners.reduce((a, c) => a + c.z, 0) / 4;
  const size = Math.hypot(room.width, room.depth);
  const ux = c1.x - c0.x, uz = c1.z - c0.z, vx = c3.x - c0.x, vz = c3.z - c0.z;
  const [ax, az] = Math.hypot(ux, uz) >= Math.hypot(vx, vz) ? [ux, uz] : [vx, vz];
  const len = Math.hypot(ax, az), dx = ax / len, dz = az / len;
  const eye = Math.min((room.ceilY ?? Infinity) - 0.2, room.floorY + 1.6);
  camera.position.set(cx - dx * len * 0.3, eye, cz - dz * len * 0.3);
  controls.target.set(cx + dx * len * 0.3, room.floorY + 0.7, cz + dz * len * 0.3);
  camera.near = 0.02;
  camera.far = size * 30;
  camera.updateProjectionMatrix();
  moveSpeed = Math.max(size * 0.2, 0.5);
  controls.update();
}

// ---------- keyboard movement ----------

// W/S forward and back, A/D sideways (level with the floor, like walking), Q/E down and up,
// Shift for faster. The orbit target moves with the camera so mouse orbiting carries on
// from wherever you've walked to.
const MOVE_KEYS = new Set(["w", "a", "s", "d", "q", "e"]);

function typing(target) {
  return target instanceof HTMLInputElement || target instanceof HTMLSelectElement ||
    target instanceof HTMLTextAreaElement || target?.isContentEditable;
}

window.addEventListener("keydown", (e) => {
  if (!visible || !splat || typing(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
  const key = e.key.toLowerCase();
  if (MOVE_KEYS.has(key)) {
    held.add(key);
    e.preventDefault();
    e.stopPropagation(); // keep the page's own shortcuts (B for bypass) out of it
  }
  if (e.key === "Shift") held.add("shift");
}, true);
window.addEventListener("keyup", (e) => {
  held.delete(e.key.toLowerCase());
  if (e.key === "Shift") held.delete("shift");
});
window.addEventListener("blur", () => held.clear());

// Move the camera (and the orbit target with it) by metres forward, right and up, where
// forward and right are level with the floor, like walking.
function moveBy(forwardM, rightM, upM) {
  const forward = new THREE.Vector3();
  camera.getWorldDirection(forward);
  forward.y = 0;
  if (forward.lengthSq() < 1e-8) forward.set(0, 0, -1).applyQuaternion(camera.quaternion).setY(0);
  forward.normalize();
  const right = new THREE.Vector3().crossVectors(forward, camera.up).normalize();
  const move = forward.multiplyScalar(forwardM).add(right.multiplyScalar(rightM));
  move.y += upM;
  camera.position.add(move);
  controls.target.add(move);
}

function walk(dt) {
  if (!dt || !held.size || !camera) return;
  let f = 0, r = 0, u = 0;
  if (held.has("w")) f += 1;
  if (held.has("s")) f -= 1;
  if (held.has("d")) r += 1;
  if (held.has("a")) r -= 1;
  if (held.has("e")) u += 1;
  if (held.has("q")) u -= 1;
  const len = Math.hypot(f, r, u);
  if (!len) return;
  const step = moveSpeed * dt * (held.has("shift") ? 3 : 1) / len;
  moveBy(f * step, r * step, u * step);
}

// Scrolling moves through the scene instead of scrolling the page: swipe up/down to go
// forward/back, left/right to step sideways; pinch (which browsers send as ctrl+wheel) zooms
// toward the orbit point.
function onWheel(e) {
  e.preventDefault();
  if (!splat) return;
  const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1; // lines/pages to pixels
  const dx = e.deltaX * unit, dy = e.deltaY * unit;
  if (e.ctrlKey) {
    const offset = camera.position.clone().sub(controls.target);
    const dist = Math.max(offset.length() * Math.exp(dy * 0.01), 0.05);
    camera.position.copy(controls.target).add(offset.setLength(dist));
    return;
  }
  const perPixel = moveSpeed * 0.004 * (e.shiftKey ? 3 : 1); // metres per scrolled pixel
  moveBy(-dy * perPixel, dx * perPixel, 0);
}

// ---------- drop / open ----------

const stage = $("scan-stage"), drop = $("scan-drop");
stage.addEventListener("dragover", (e) => { e.preventDefault(); drop.hidden = false; drop.classList.add("over"); });
stage.addEventListener("dragleave", () => { drop.classList.remove("over"); if (splat) drop.hidden = true; });
stage.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  load(e.dataTransfer.files[0]);
});
// Dropping anywhere on the room panel works too, so you don't have to find the stage first.
$("room").addEventListener("dragover", (e) => e.preventDefault());
$("room").addEventListener("drop", (e) => {
  if (stage.contains(e.target)) return;
  e.preventDefault();
  load(e.dataTransfer.files[0]);
});
$("scan-open").onclick = () => $("scan-file").click();
$("scan-file").addEventListener("change", (e) => load(e.target.files[0]));

// ---------- picking ----------

function setCurrent(key) {
  current = key;
  document.querySelectorAll("[data-pick]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.pick === key)));
  if (splat) status(`Click to place the ${MARKERS[key].name.toLowerCase()}`);
}
document.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = () => setCurrent(b.dataset.pick)));

// ---------- X/Y/Z move handles ----------

function initGizmo() {
  gizmo = new TransformControls(camera, renderer.domElement);
  gizmo.setMode("translate");
  gizmo.setSpace("world"); // X across, Y up, Z depth, whatever the marker's parent is doing
  gizmo.setSize(0.8);
  // Don't orbit the camera while a handle is being dragged; save when the drag ends.
  gizmo.addEventListener("dragging-changed", (e) => {
    controls.enabled = !e.value;
    if (!e.value) save();
  });
  gizmo.addEventListener("objectChange", () => {
    const m = gizmo.object;
    if (!m) return;
    picks[m.userData.key] = m.position.clone(); // markers live in the splat's local frame
    update();
  });
  // Splats draw in the transparent pass; keep the handles on top of them.
  const helper = gizmo.getHelper();
  helper.traverse((o) => { o.renderOrder = 20; });
  scene.add(helper);
}

function select(key) {
  const m = markerObjs[key];
  if (gizmo && m) gizmo.attach(m);
}

function wirePicking() {
  const canvas = renderer.domElement;
  let down = null;
  canvas.addEventListener("pointerdown", (e) => {
    // gizmo.axis is set while the pointer is over one of its handles
    down = [e.clientX, e.clientY, e.button, Boolean(gizmo?.axis)];
  });
  canvas.addEventListener("pointerup", (e) => {
    if (!splat || !down || down[2] !== 0 || down[3]) return; // not a left click, or a handle drag
    if (Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return; // it was an orbit drag
    const rect = canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((e.clientX - rect.left) / rect.width) * 2 - 1,
      -((e.clientY - rect.top) / rect.height) * 2 + 1,
    );
    // A fresh raycaster per click: the surface lookup runs a frame later, and a shared one
    // would be re-aimed by a quick second click before the first lookup runs.
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(ndc, camera);
    const target = current; // likewise, the marker this click is for
    // Clicking a marker selects it for fine adjustment instead of re-placing anything.
    const markerHit = raycaster.intersectObjects(Object.values(markerObjs), false)[0];
    if (markerHit) {
      const key = markerHit.object.userData.key;
      setCurrent(key);
      select(key);
      status(`Drag the arrows to adjust the ${MARKERS[key].name.toLowerCase()}, or click the scan to re-place it.`);
      return;
    }
    status("Finding the surface…");
    // Raycasting a splat walks every point, so it only runs on click. The short timer lets
    // the status message paint first; unlike an animation frame, it also runs when the page
    // isn't visible.
    setTimeout(() => {
      const hit = raycaster.intersectObject(splat, false)[0];
      if (!hit) { status("Missed the scan; click on a surface."); return; }
      const placed = target;
      picks[placed] = splat.worldToLocal(hit.point.clone());
      save();
      update();
      select(placed);
      const next = ORDER.find((k) => !picks[k]);
      if (next) setCurrent(next);
      else status("All three placed. Check the distances, then “Use these positions”.");
    }, 30);
  });
}

// ---------- markers, measurements ----------

function world(key) {
  return splat.localToWorld(picks[key].clone());
}

function update() {
  if (!splat) return;
  // Markers are children of the splat, so they follow the flip.
  for (const key of ORDER) {
    if (!picks[key]) continue;
    let m = markerObjs[key];
    if (!m) {
      // Splats draw in the transparent pass, so markers must too (with a later render order)
      // or the scan paints over them. Sized to the scan so they read at any room size.
      const box = splat.getBoundingBox(true);
      const r = Math.max(box.getSize(new THREE.Vector3()).length() * 0.006, 0.02);
      m = new THREE.Mesh(
        new THREE.SphereGeometry(r, 24, 16),
        new THREE.MeshBasicMaterial({ color: MARKERS[key].color, depthTest: false, transparent: true, opacity: 0.95 }),
      );
      m.renderOrder = 10;
      m.userData.key = key;
      splat.add(m);
      markerObjs[key] = m;
    }
    m.position.copy(picks[key]);
  }
  if (lines) { lines.parent?.remove(lines); lines.geometry.dispose(); lines = null; }
  if (picks.seat && (picks.left || picks.right)) {
    const pts = [];
    for (const k of ["left", "right"]) if (picks[k]) pts.push(picks.seat, picks[k]);
    lines = new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: 0xffffff, depthTest: false, transparent: true, opacity: 0.8 }),
    );
    lines.renderOrder = 9;
    splat.add(lines);
  }
  measure();
}

// Floor-plane layout in metres: origin at the left HomePod, x toward the right one, y into the
// room on the seat's side. "Up" is world +Y once the scan is upright.
function layout() {
  if (!(picks.left && picks.right && picks.seat)) return null;
  const L = world("left"), R = world("right"), S = world("seat");
  const flat = (v) => new THREE.Vector2(v.x, v.z);
  const l = flat(L), r = flat(R), s = flat(S);
  const span = r.clone().sub(l);
  const spanLen = span.length();
  if (spanLen < 1e-6) return null;
  const xh = span.clone().divideScalar(spanLen);
  let yh = new THREE.Vector2(-xh.y, xh.x);
  const v = s.clone().sub(l);
  let sy = v.dot(yh);
  if (sy < 0) { yh.negate(); sy = -sy; }
  const real = parseFloat($("scan-real").value);
  const scale = real > 0 ? real / spanLen : 1;
  // The detected room's corners in the same plan coordinates.
  const roomCorners = room && room.corners.map((c) => {
    const d = new THREE.Vector2(c.x, c.z).sub(l);
    return { x: d.dot(xh) * scale, y: d.dot(yh) * scale };
  });
  return {
    scale, spanLen, roomCorners,
    plan: {
      left: { x: 0, y: 0 },
      right: { x: spanLen * scale, y: 0 },
      seat: { x: v.dot(xh) * scale, y: sy * scale },
    },
    d3: { left: S.distanceTo(L) * scale, right: S.distanceTo(R) * scale },
    heights: { left: (L.y - S.y) * scale, right: (R.y - S.y) * scale },
  };
}

function roomSummary(scale) {
  if (!room) return "";
  const f = (v) => (v * scale).toFixed(2);
  const missing = 4 - room.wallsFound;
  return `Room: <b>${f(room.width)} × ${f(room.depth)} m</b>` +
    (room.height !== null ? `, ceiling ${f(room.height)} m` : ", no ceiling in the scan") +
    (missing ? ` (${missing} wall${missing === 1 ? "" : "s"} estimated from the floor's edge)` : "") + ". ";
}

function measure() {
  const out = $("scan-dist");
  const placed = ORDER.filter((k) => picks[k]).map((k) => MARKERS[k].name);
  const lay = layout();
  $("scan-apply").disabled = !lay;
  if (!lay) {
    out.innerHTML = roomSummary(1) + (placed.length ? `Placed: ${placed.join(", ")}.` : "");
    $("scan-scale").textContent = "";
    return;
  }
  $("scan-scale").textContent = lay.scale === 1
    ? `Scan says ${lay.spanLen.toFixed(2)} m. LiDAR scans are usually right; enter a tape measurement to correct.`
    : `Scan said ${lay.spanLen.toFixed(2)} m, so everything is scaled ×${lay.scale.toFixed(3)}.`;
  const floor = (k) => Math.hypot(lay.plan.seat.x - lay.plan[k].x, lay.plan.seat.y - lay.plan[k].y);
  const h = (k) => `${lay.heights[k] >= 0 ? "+" : ""}${lay.heights[k].toFixed(2)} m`;
  out.innerHTML = roomSummary(lay.scale) +
    `To the left HomePod: <b>${lay.d3.left.toFixed(2)} m</b> (${floor("left").toFixed(2)} m along the floor, height ${h("left")}). ` +
    `To the right HomePod: <b>${lay.d3.right.toFixed(2)} m</b> (${floor("right").toFixed(2)} m along the floor, height ${h("right")}).`;
}
$("scan-real").addEventListener("input", () => { save(); measure(); });

$("scan-apply").onclick = () => {
  const lay = layout();
  if (!lay || !window.sbRoom) return;
  window.sbRoom.setPositions(lay.plan);
  if (lay.roomCorners) window.sbRoom.setRoom(lay.roomCorners);
  showView(false);
};

// ---------- persistence (per scan file, this browser only) ----------

function save() {
  if (!fileKey) return;
  const data = { picks: {}, real: $("scan-real").value, flip: $("scan-flip").checked };
  for (const k of ORDER) if (picks[k]) data.picks[k] = picks[k].toArray();
  try { localStorage.setItem(fileKey, JSON.stringify(data)); } catch { /* storage unavailable */ }
}

function restore() {
  gizmo?.detach();
  for (const m of Object.values(markerObjs)) { m.parent?.remove(m); m.geometry.dispose(); }
  markerObjs = {};
  picks = {};
  let data = null;
  try { data = JSON.parse(localStorage.getItem(fileKey) || "null"); } catch { /* ignore */ }
  if (data) {
    for (const [k, arr] of Object.entries(data.picks || {})) picks[k] = new THREE.Vector3().fromArray(arr);
    $("scan-real").value = data.real || "";
    if (typeof data.flip === "boolean" && data.flip !== $("scan-flip").checked) {
      $("scan-flip").checked = data.flip;
      applyFlip();
    }
    orientationChecked = typeof data.flip === "boolean"; // a saved orientation wins
  } else {
    $("scan-real").value = "";
  }
  update();
  setCurrent(ORDER.find((k) => !picks[k]) || "left");
}

// Debug handle for scripted tests (project known points to the screen, inspect picks).
window.sbScanDebug = {
  get THREE() { return THREE; }, get camera() { return camera; }, get splat() { return splat; },
  get canvas() { return renderer?.domElement; }, get picks() { return picks; }, get gizmo() { return gizmo; }, get room() { return room; }, layout,
  renderOnce() { controls.update(); renderer.render(scene, camera); },
  walk, held,
};
