// 3D scan view: render a Gaussian splat of the room (from Scaniverse, Polycam, Luma...) with Spark,
// let the user click the two HomePods and their head position on it, and copy the measured
// layout to the plan.
//
// three.js and Spark are only downloaded when this view is first opened. The scan file never
// leaves the browser; placed markers are remembered per file in localStorage.

const $ = (id) => document.getElementById(id);
const MARKERS = {
  left: { name: "Left HomePod", color: 0x5b8ff0 },
  right: { name: "Right HomePod", color: 0xe39045 },
  seat: { name: "Your head", color: 0x5cc07f },
};
const ORDER = ["left", "right", "seat"];

let THREE, OrbitControls, SplatMesh, SparkRenderer;
let renderer, scene, camera, controls;
let splat = null; // current SplatMesh
let fileKey = null; // localStorage key for the current file
let picks = {}; // marker -> THREE.Vector3 in the splat's local frame
let markerObjs = {};
let lines = null;
let current = "left";
let visible = false;

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

function frame() {
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
  fileKey = `offcentre-scan:${file.name}:${file.size}`;
  restore();
  frameCamera();
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
$("scan-flip").addEventListener("change", () => { applyFlip(); if (splat) { frameCamera(); update(); } });

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

function wirePicking() {
  const canvas = renderer.domElement;
  const raycaster = new THREE.Raycaster();
  let down = null;
  canvas.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY, e.button]; });
  canvas.addEventListener("pointerup", (e) => {
    if (!splat || !down || down[2] !== 0) return;
    if (Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return; // it was an orbit drag
    const rect = canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((e.clientX - rect.left) / rect.width) * 2 - 1,
      -((e.clientY - rect.top) / rect.height) * 2 + 1,
    );
    raycaster.setFromCamera(ndc, camera);
    status("Finding the surface…");
    // Raycasting a splat walks every point, so it only runs on click.
    requestAnimationFrame(() => {
      const hit = raycaster.intersectObject(splat, false)[0];
      if (!hit) { status("Missed the scan; click on a surface."); return; }
      picks[current] = splat.worldToLocal(hit.point.clone());
      save();
      update();
      const next = ORDER.find((k) => !picks[k]);
      if (next) setCurrent(next);
      else status("All three placed. Check the distances, then “Use these positions”.");
    });
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
  return {
    scale, spanLen,
    plan: {
      left: { x: 0, y: 0 },
      right: { x: spanLen * scale, y: 0 },
      seat: { x: v.dot(xh) * scale, y: sy * scale },
    },
    d3: { left: S.distanceTo(L) * scale, right: S.distanceTo(R) * scale },
    heights: { left: (L.y - S.y) * scale, right: (R.y - S.y) * scale },
  };
}

function measure() {
  const out = $("scan-dist");
  const placed = ORDER.filter((k) => picks[k]).map((k) => MARKERS[k].name);
  const lay = layout();
  $("scan-apply").disabled = !lay;
  if (!lay) {
    out.textContent = placed.length ? `Placed: ${placed.join(", ")}.` : "";
    $("scan-scale").textContent = "";
    return;
  }
  $("scan-scale").textContent = lay.scale === 1
    ? `Scan says ${lay.spanLen.toFixed(2)} m. LiDAR scans are usually right; enter a tape measurement to correct.`
    : `Scan said ${lay.spanLen.toFixed(2)} m, so everything is scaled ×${lay.scale.toFixed(3)}.`;
  const floor = (k) => Math.hypot(lay.plan.seat.x - lay.plan[k].x, lay.plan.seat.y - lay.plan[k].y);
  const h = (k) => `${lay.heights[k] >= 0 ? "+" : ""}${lay.heights[k].toFixed(2)} m`;
  out.innerHTML =
    `To the left HomePod: <b>${lay.d3.left.toFixed(2)} m</b> (${floor("left").toFixed(2)} m along the floor, height ${h("left")}). ` +
    `To the right HomePod: <b>${lay.d3.right.toFixed(2)} m</b> (${floor("right").toFixed(2)} m along the floor, height ${h("right")}).`;
}
$("scan-real").addEventListener("input", () => { save(); measure(); });

$("scan-apply").onclick = () => {
  const lay = layout();
  if (!lay || !window.sbRoom) return;
  window.sbRoom.setPositions(lay.plan);
  showView(false);
};

// ---------- persistence (per scan file, this browser only) ----------

function save() {
  if (!fileKey) return;
  const data = { picks: {}, real: $("scan-real").value };
  for (const k of ORDER) if (picks[k]) data.picks[k] = picks[k].toArray();
  try { localStorage.setItem(fileKey, JSON.stringify(data)); } catch { /* storage unavailable */ }
}

function restore() {
  for (const m of Object.values(markerObjs)) { m.parent?.remove(m); m.geometry.dispose(); }
  markerObjs = {};
  picks = {};
  let data = null;
  try { data = JSON.parse(localStorage.getItem(fileKey) || "null"); } catch { /* ignore */ }
  if (data) {
    for (const [k, arr] of Object.entries(data.picks || {})) picks[k] = new THREE.Vector3().fromArray(arr);
    $("scan-real").value = data.real || "";
  } else {
    $("scan-real").value = "";
  }
  update();
  setCurrent(ORDER.find((k) => !picks[k]) || "left");
}

// Debug handle for scripted tests (project known points to the screen, inspect picks).
window.sbScanDebug = {
  get THREE() { return THREE; }, get camera() { return camera; }, get splat() { return splat; },
  get canvas() { return renderer?.domElement; }, get picks() { return picks; }, layout,
};
