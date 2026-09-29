// Room plan: a to-scale SVG of the two HomePods and the listening seat.
//
// Units inside the SVG are centimetres, so the viewBox is the real room at 1:1 and the browser
// only scales it uniformly. x runs along the speaker wall from the left HomePod, y into the room
// (down the screen). Settings store metres.

const SPEED = 343; // m/s
const POD_R = 7.1; // HomePod (2nd gen) is 142 mm across
const SEAT_R = 10; // roughly a head, seen from above
const HIT_R = 32; // generous grab area
const MAX_DELAY_MS = 50;
const MIN_GAIN_DB = -60;
const NS = "http://www.w3.org/2000/svg";

const sb = window.sb;
const $ = (id) => document.getElementById(id);
const svg = $("plan");

const ITEMS = [
  { key: "left", name: "Left HomePod", cls: "pod" },
  { key: "right", name: "Right HomePod", cls: "pod" },
  { key: "seat", name: "You", cls: "seat" },
];

let pos = null; // {left:{x,y}, right:{x,y}, seat:{x,y}} in metres, local copy
let view = null; // {x0, y0, w, h} in cm
let dragging = null;

function el(tag, attrs = {}, parent = svg) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  parent.appendChild(node);
  return node;
}

// ---------- geometry ----------

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

function correction(p, law) {
  const dl = dist(p.seat, p.left), dr = dist(p.seat, p.right);
  if (dl < 0.05 || dr < 0.05) return null;
  const nearSide = dl <= dr ? "left" : "right";
  const farSide = nearSide === "left" ? "right" : "left";
  const near = Math.min(dl, dr), far = Math.max(dl, dr);
  const delay = Math.min((far - near) / SPEED * 1000, MAX_DELAY_MS);
  const gain = Math.max(20 * Math.log10(near / far) * law, MIN_GAIN_DB);
  return {
    nearSide, dl, dr,
    changes: {
      [`${nearSide}_delay_ms`]: +delay.toFixed(3), [`${nearSide}_gain_db`]: +gain.toFixed(2),
      [`${farSide}_delay_ms`]: 0, [`${farSide}_gain_db`]: 0,
    },
  };
}

function posFromSettings(s) {
  return {
    left: { x: s.left_x, y: s.left_y },
    right: { x: s.right_x, y: s.right_y },
    seat: { x: s.seat_x, y: s.seat_y },
  };
}

function posChanges(key) {
  return { [`${key}_x`]: +pos[key].x.toFixed(3), [`${key}_y`]: +pos[key].y.toFixed(3) };
}

// Send position changes, plus the derived delay/level when the room drives the correction.
function push(changes, immediate) {
  const s = sb.state.settings;
  if (s.follow_room) {
    const c = correction(pos, s.level_law);
    if (c) Object.assign(changes, c.changes);
  }
  sb.queue(changes, immediate);
}

// ---------- view ----------

function fitView() {
  const pts = Object.values(pos);
  const xs = pts.map((p) => p.x * 100), ys = pts.map((p) => p.y * 100);
  const pad = 90;
  let x0 = Math.min(...xs) - pad, x1 = Math.max(...xs) + pad;
  let y0 = Math.min(...ys) - pad, y1 = Math.max(...ys) + pad;
  // Keep a sensible minimum area and aspect so a tight layout isn't blown up to fill the page.
  const minW = 400, minH = 280;
  if (x1 - x0 < minW) { const c = (x0 + x1) / 2; x0 = c - minW / 2; x1 = c + minW / 2; }
  if (y1 - y0 < minH) { const c = (y0 + y1) / 2; y0 = c - minH / 2; y1 = c + minH / 2; }
  if ((y1 - y0) < (x1 - x0) * 0.45) { const c = (y0 + y1) / 2, h = (x1 - x0) * 0.45; y0 = c - h / 2; y1 = c + h / 2; }
  // Snap to whole 50 cm so grid lines land on the edges.
  x0 = Math.floor(x0 / 50) * 50; y0 = Math.floor(y0 / 50) * 50;
  x1 = Math.ceil(x1 / 50) * 50; y1 = Math.ceil(y1 / 50) * 50;
  const next = { x0, y0, w: x1 - x0, h: y1 - y0 };
  if (view && next.x0 === view.x0 && next.y0 === view.y0 && next.w === view.w && next.h === view.h) return;
  view = next;
  svg.setAttribute("viewBox", `${view.x0} ${view.y0} ${view.w} ${view.h}`);
  drawGrid();
}

// Stroke widths and text are specified in screen pixels; convert to cm for the current scale.
function pxToCm() {
  const width = svg.clientWidth || 800;
  return view.w / width;
}

let gridLayer, rayLayer, itemLayer;
const shapes = {};

function build() {
  svg.replaceChildren();
  gridLayer = el("g");
  rayLayer = el("g");
  itemLayer = el("g");

  shapes.span = el("line", { class: "span" }, rayLayer);
  shapes.rayL = el("line", { class: "ray left" }, rayLayer);
  shapes.rayR = el("line", { class: "ray right" }, rayLayer);
  shapes.labelL = el("text", { class: "dim-label left", "text-anchor": "middle" }, rayLayer);
  shapes.labelR = el("text", { class: "dim-label right", "text-anchor": "middle" }, rayLayer);
  shapes.labelSpan = el("text", { class: "dim-label span", "text-anchor": "middle" }, rayLayer);

  for (const item of ITEMS) {
    const g = el("g", { class: "handle", tabindex: "0", "data-key": item.key }, itemLayer);
    const hit = el("circle", { class: "hit" }, g);
    const halo = el("circle", { class: "halo" }, g);
    const body = el("circle", { class: item.cls }, g);
    const name = el("text", { class: "name", "text-anchor": "middle" }, g);
    name.textContent = item.name;
    const coord = el("text", { class: "coord", "text-anchor": "middle" }, g);
    shapes[item.key] = { g, hit, halo, body, name, coord, r: item.cls === "pod" ? POD_R : SEAT_R };
    g.addEventListener("pointerdown", (e) => startDrag(e, item.key));
    g.addEventListener("keydown", (e) => nudge(e, item.key));
  }
}

function drawGrid() {
  gridLayer.replaceChildren();
  const k = pxToCm();
  const { x0, y0, w, h } = view;
  for (let x = Math.ceil(x0 / 50) * 50; x <= x0 + w; x += 50) {
    const major = x % 100 === 0;
    el("line", { x1: x, y1: y0, x2: x, y2: y0 + h, class: major ? "grid-major" : "grid-minor",
      "vector-effect": "non-scaling-stroke" }, gridLayer);
    if (major) {
      const t = el("text", { x: x + 4 * k, y: y0 + 14 * k, class: "axis-label", "font-size": 12 * k }, gridLayer);
      t.textContent = `${x / 100} m`;
    }
  }
  for (let y = Math.ceil(y0 / 50) * 50; y <= y0 + h; y += 50) {
    const major = y % 100 === 0;
    el("line", { x1: x0, y1: y, x2: x0 + w, y2: y, class: major ? "grid-major" : "grid-minor",
      "vector-effect": "non-scaling-stroke" }, gridLayer);
    if (major && y !== 0) {
      const t = el("text", { x: x0 + 4 * k, y: y - 4 * k, class: "axis-label", "font-size": 12 * k }, gridLayer);
      t.textContent = `y ${y / 100} m`;
    }
  }
  // origin marker label
  const t = el("text", { x: -12 * k, y: 4 * k, class: "axis-label", "font-size": 12 * k,
    "text-anchor": "end" }, gridLayer);
  t.textContent = "origin";
}

function setLine(line, a, b) {
  line.setAttribute("x1", a.x * 100); line.setAttribute("y1", a.y * 100);
  line.setAttribute("x2", b.x * 100); line.setAttribute("y2", b.y * 100);
}

function setLabel(text, a, b, str, k, offsetPx) {
  // Midpoint, pushed perpendicular to the line so it doesn't sit on top of it.
  const mx = (a.x + b.x) * 50, my = (a.y + b.y) * 50;
  let nx = -(b.y - a.y), ny = b.x - a.x;
  const n = Math.hypot(nx, ny) || 1;
  nx /= n; ny /= n;
  text.setAttribute("x", mx + nx * offsetPx * k);
  text.setAttribute("y", my + ny * offsetPx * k + 4 * k);
  text.setAttribute("font-size", 13 * k);
  text.textContent = str;
}

function draw() {
  if (!pos || !view) return;
  const k = pxToCm();
  const { left, right, seat } = pos;

  setLine(shapes.span, left, right);
  setLine(shapes.rayL, seat, left);
  setLine(shapes.rayR, seat, right);
  for (const line of [shapes.span, shapes.rayL, shapes.rayR]) {
    line.setAttribute("stroke-width", (line === shapes.span ? 1.4 : 2.4) * k);
    line.setAttribute("stroke-dasharray", line === shapes.span ? `${6 * k} ${5 * k}` : "");
  }
  setLabel(shapes.labelL, seat, left, `${dist(seat, left).toFixed(2)} m`, k, 14);
  setLabel(shapes.labelR, seat, right, `${dist(seat, right).toFixed(2)} m`, k, -14);
  setLabel(shapes.labelSpan, left, right, `${dist(left, right).toFixed(2)} m apart`, k, -16);

  for (const item of ITEMS) {
    const sh = shapes[item.key], p = pos[item.key];
    const cx = p.x * 100, cy = p.y * 100;
    // Draw true size, but never smaller than 5 px so it stays visible and grabbable.
    const r = Math.max(sh.r, 5 * k);
    for (const [c, rad] of [[sh.hit, Math.max(HIT_R, 16 * k)], [sh.halo, r + 5 * k], [sh.body, r]]) {
      c.setAttribute("cx", cx); c.setAttribute("cy", cy); c.setAttribute("r", rad);
    }
    sh.halo.setAttribute("stroke-width", 2 * k);
    sh.name.setAttribute("x", cx); sh.name.setAttribute("y", cy - r - 9 * k);
    sh.name.setAttribute("font-size", 13 * k);
    sh.coord.setAttribute("x", cx); sh.coord.setAttribute("y", cy + r + 17 * k);
    sh.coord.setAttribute("font-size", 11.5 * k);
    sh.coord.textContent = `x ${p.x.toFixed(2)}  y ${p.y.toFixed(2)}`;
    sh.g.setAttribute("aria-label", `${item.name} at x ${p.x.toFixed(2)} m, y ${p.y.toFixed(2)} m`);
  }
}

// ---------- interaction ----------

function toRoom(e) {
  const pt = svg.createSVGPoint();
  pt.x = e.clientX; pt.y = e.clientY;
  const p = pt.matrixTransform(svg.getScreenCTM().inverse());
  return { x: Math.round(p.x) / 100, y: Math.round(p.y) / 100 }; // 1 cm resolution
}

function busyKeys(key, on) {
  for (const k of [`${key}_x`, `${key}_y`]) sb.setBusy(k, on);
}

function startDrag(e, key) {
  e.preventDefault();
  const g = shapes[key].g;
  g.setPointerCapture(e.pointerId);
  g.classList.add("dragging");
  const start = toRoom(e), origin = { ...pos[key] };
  dragging = { key, start, origin };
  busyKeys(key, true);
  const move = (ev) => {
    const now = toRoom(ev);
    pos[key] = {
      x: +(origin.x + now.x - start.x).toFixed(2),
      y: +(origin.y + now.y - start.y).toFixed(2),
    };
    draw(); renderTable();
    push(posChanges(key));
  };
  const up = () => {
    g.removeEventListener("pointermove", move);
    g.removeEventListener("pointerup", up);
    g.removeEventListener("pointercancel", up);
    g.classList.remove("dragging");
    dragging = null;
    push(posChanges(key), true);
    setTimeout(() => busyKeys(key, false), 400);
    fitView(); draw();
  };
  g.addEventListener("pointermove", move);
  g.addEventListener("pointerup", up);
  g.addEventListener("pointercancel", up);
}

function nudge(e, key) {
  const step = e.shiftKey ? 0.1 : 0.01;
  const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
  if (!d) return;
  e.preventDefault();
  pos[key] = { x: +(pos[key].x + d[0]).toFixed(2), y: +(pos[key].y + d[1]).toFixed(2) };
  fitView(); draw(); renderTable();
  push(posChanges(key));
}

// ---------- coordinates table ----------

const tbody = $("coords");
const rows = {};
for (const item of ITEMS) {
  const tr = document.createElement("tr");
  const colour = item.key === "left" ? "var(--accent)" : item.key === "right" ? "var(--warn)" : "var(--ok)";
  tr.innerHTML = `
    <td><span class="sw" style="background:${colour}"></span>${item.name}</td>
    <td class="num"><input type="number" step="0.01" data-axis="x" aria-label="${item.name} x in metres"></td>
    <td class="num"><input type="number" step="0.01" data-axis="y" aria-label="${item.name} y in metres"></td>
    <td class="num" data-f="d"></td><td class="num" data-f="dx"></td><td class="num" data-f="dy"></td>`;
  tbody.appendChild(tr);
  rows[item.key] = tr;
  for (const input of tr.querySelectorAll("input")) {
    const axis = input.dataset.axis, busyKey = `${item.key}_${axis}`;
    input.addEventListener("focus", () => sb.setBusy(busyKey, true));
    input.addEventListener("blur", () => setTimeout(() => sb.setBusy(busyKey, false), 400));
    input.addEventListener("change", () => {
      const v = parseFloat(input.value);
      if (!Number.isFinite(v)) return;
      pos[item.key] = { ...pos[item.key], [axis]: Math.max(-30, Math.min(30, v)) };
      fitView(); draw(); renderTable();
      push(posChanges(item.key), true);
    });
  }
}

function renderTable() {
  for (const item of ITEMS) {
    const tr = rows[item.key], p = pos[item.key];
    for (const input of tr.querySelectorAll("input")) {
      if (document.activeElement !== input) input.value = p[input.dataset.axis].toFixed(2);
    }
    const cells = Object.fromEntries([...tr.querySelectorAll("[data-f]")].map((c) => [c.dataset.f, c]));
    if (item.key === "seat") {
      cells.d.textContent = ""; cells.dx.textContent = ""; cells.dy.textContent = "";
    } else {
      cells.d.textContent = `${dist(pos.seat, p).toFixed(2)} m`;
      cells.dx.textContent = `${(pos.seat.x - p.x).toFixed(2)}`;
      cells.dy.textContent = `${(pos.seat.y - p.y).toFixed(2)}`;
    }
  }
  renderSummary();
}

function renderSummary() {
  const s = sb.state.settings;
  const c = correction(pos, s.level_law);
  const out = $("room-summary");
  if (!c) { out.textContent = "Your seat is on top of a speaker."; return; }
  const near = c.nearSide, far = near === "left" ? "right" : "left";
  const cap = (w) => w[0].toUpperCase() + w.slice(1);
  const ms = (Math.abs(c.dr - c.dl) / SPEED * 1000).toFixed(2);
  let text = `${cap(near)} is ${Math.min(c.dl, c.dr).toFixed(2)} m away, ${far} ${Math.max(c.dl, c.dr).toFixed(2)} m: ` +
    `sound from the ${far} arrives ${ms} ms later. `;
  if (s.follow_room) {
    text += `So the ${near} HomePod is delayed ${c.changes[`${near}_delay_ms`].toFixed(2)} ms and set to ` +
      `${c.changes[`${near}_gain_db`].toFixed(1)} dB.`;
  } else {
    text += "Delay and level are currently set by hand; tick “Set delay & level from room” to use the plan.";
  }
  out.textContent = text;
}

// ---------- options ----------

$("follow_room").addEventListener("change", (e) => {
  sb.state.settings.follow_room = e.target.checked;
  push({ follow_room: e.target.checked }, true);
});
$("level_law").addEventListener("change", (e) => {
  sb.state.settings.level_law = +e.target.value;
  push({ level_law: +e.target.value }, true);
});

// Lets the 3D scan view place everything at once, with the same derived correction.
window.sbRoom = {
  setPositions(next) {
    for (const key of ["left", "right", "seat"]) pos[key] = { x: +next[key].x.toFixed(2), y: +next[key].y.toFixed(2) };
    fitView(); draw(); renderTable();
    push({ ...posChanges("left"), ...posChanges("right"), ...posChanges("seat") }, true);
  },
};

// ---------- sync with server state ----------

build();

sb.onRender((state) => {
  const s = state.settings;
  const incoming = posFromSettings(s);
  if (!pos) {
    pos = incoming;
  } else if (!dragging) {
    // Take server values except for fields being edited here.
    for (const key of ["left", "right", "seat"]) {
      for (const axis of ["x", "y"]) {
        if (!sb.isBusy(`${key}_${axis}`)) pos[key][axis] = incoming[key][axis];
      }
    }
  }
  $("follow_room").checked = s.follow_room;
  const law = $("level_law");
  if (document.activeElement !== law) {
    const opt = [...law.options].find((o) => Math.abs(+o.value - s.level_law) < 1e-6);
    if (opt) law.value = opt.value;
  }
  if (!dragging) fitView();
  draw();
  renderTable();
});

new ResizeObserver(() => { if (view) { drawGrid(); draw(); } }).observe(svg);
