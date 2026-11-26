// Timeline map: a strip beside the Timeline with a mark per notable row, placed by row index over
// the current row list, and a box over the rows in view. Click jumps, drag scrubs. Loaded as a
// module script after timeline_window.js, which supplies the row and visible-range accessors.
const api = window.sessionTimeline;
const canvas = document.getElementById("timeline-map");
if (api && api.getRows && canvas) {
  const box = document.getElementById("timeline-box");
  const tip = document.getElementById("timeline-map-tip");
  const MIN_MARK = 2;     // CSS px: every mark stays visible however long the session
  const HIT_SLOP = 3;     // CSS px around a mark that still counts as hovering it

  // Mark kinds: kind -> colour token. A later kind adds one entry here, and `segment` places its
  // mark in the strip (all prompts share the one segment for now).
  const MARK_KINDS = {
    prompt: { token: "--accent", segment: 0 },
  };
  function markKind(row) { return MARK_KINDS[row.kind] ? row.kind : null; }
  function markLabel(row) { return String(row.text || "").split("\n")[0]; }

  let marks = [];         // {index, kind, row} for the rows that have a mark, in row order
  let count = 0;          // rows in the list
  let fits = true;
  let dragging = false;
  let width = 0;
  let height = 0;
  let observedSpace = null;

  function token(name) { return window.theme ? window.theme.token(name) : getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function yOf(index) { return count ? index / count * height : 0; }

  function drawMarks(ctx) {
    marks.forEach(function (m) {
      ctx.fillStyle = token(MARK_KINDS[m.kind].token);
      const y = yOf(m.index);
      ctx.fillRect(0, y, width, Math.max(MIN_MARK, yOf(m.index + 1) - y));
    });
  }

  // The box over the rows in view.
  function drawView(ctx) {
    const r = api.getVisibleRange();
    if (!count || r.start < 0) { return; }
    const y = yOf(r.start);
    const h = Math.max(MIN_MARK, yOf(r.end + 1) - y);
    ctx.globalAlpha = 0.18;
    ctx.fillStyle = token("--text");
    ctx.fillRect(0, y, width, h);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = token("--muted");
    ctx.lineWidth = 1;
    ctx.strokeRect(0.5, y + 0.5, width - 1, Math.max(h - 1, 1));
  }

  function draw() {
    if (canvas.hidden || !width || !height) { return; }
    const dpr = window.devicePixelRatio || 1;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    drawMarks(ctx);
    // The search strip (#152) is drawn here, between the marks and the box.
    drawView(ctx);
  }

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    draw();
  }

  // The map is hidden while the whole Timeline fits in its box without scrolling.
  function updateFit() {
    const nowFits = api.body.scrollHeight <= api.body.clientHeight;
    if (nowFits === fits && !canvas.hidden === !nowFits) { return; }
    fits = nowFits;
    canvas.hidden = fits;
    box.classList.toggle("has-map", !fits);
    if (!fits) { resize(); }
  }

  // The single entry point: re-derive the marks from the current row list and redraw.
  function rebuild() {
    const rows = api.getRows();
    count = rows.length;
    marks = [];
    rows.forEach(function (row, index) {
      const kind = markKind(row);
      if (kind) { marks.push({ index: index, kind: kind, row: row }); }
    });
    const space = api.body.firstElementChild;
    if (space && space !== observedSpace && window.ResizeObserver) {  // rows re-measured: the fit can change
      observedSpace = space;
      new ResizeObserver(updateFit).observe(space);
    }
    updateFit();
    draw();
  }

  function indexAt(event) {
    const y = event.clientY - canvas.getBoundingClientRect().top;
    return Math.max(0, Math.min(count - 1, Math.floor(y / height * count)));
  }
  function jump(event) { if (count) { api.scrollToIndex(indexAt(event), { align: "start" }); } }

  function markAt(event) {
    const y = event.clientY - canvas.getBoundingClientRect().top;
    let best = null;
    let bestDist = HIT_SLOP + 1;
    marks.forEach(function (m) {
      const top = yOf(m.index);
      const bottom = top + Math.max(MIN_MARK, yOf(m.index + 1) - top);
      const dist = y < top ? top - y : (y > bottom ? y - bottom : 0);
      if (dist < bestDist) { best = m; bestDist = dist; }
    });
    return best;
  }
  function showTip(event) {
    const m = dragging ? null : markAt(event);
    if (!m) { tip.hidden = true; return; }
    tip.textContent = markLabel(m.row);
    tip.style.top = (event.clientY - tip.parentNode.getBoundingClientRect().top) + "px";
    tip.hidden = false;
  }

  canvas.addEventListener("pointerdown", function (event) {
    dragging = true;
    canvas.setPointerCapture(event.pointerId);
    tip.hidden = true;
    jump(event);
  });
  canvas.addEventListener("pointermove", function (event) {
    if (dragging) { jump(event); } else { showTip(event); }
  });
  function release(event) {
    dragging = false;
    if (canvas.hasPointerCapture(event.pointerId)) { canvas.releasePointerCapture(event.pointerId); }
  }
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);
  canvas.addEventListener("pointerleave", function () { tip.hidden = true; });

  api.onRowsChange(rebuild);
  api.onVisibleRangeChange(function () { updateFit(); draw(); });
  if (window.ResizeObserver) {
    new ResizeObserver(function () { updateFit(); if (!canvas.hidden) { resize(); } }).observe(api.body);
  }
  if (window.matchMedia) {  // the tokens change with the theme; there is no other theme signal
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
  }
  window.timelineMap = { rebuild: rebuild };
  if (api.getRows().length) { rebuild(); }
}
