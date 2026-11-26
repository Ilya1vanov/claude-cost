// Timeline map: a strip beside the Timeline with a mark per notable row, placed by row index over
// the current row list, and a box over the rows in view. Click jumps, drag scrubs. Loaded as a
// module script after timeline_window.js, which supplies the row and visible-range accessors.
const api = window.sessionTimeline;
const canvas = document.getElementById("timeline-map");
if (api && api.getRows && canvas) {
  const box = document.getElementById("timeline-box");
  const tip = document.getElementById("timeline-map-tip");
  const MIN_MARK = 2;     // CSS px: every mark stays visible however long the session
  const SEARCH_W = 4;     // CSS px: the search strip on the map's right edge, outside the marks and the box
  const HIT_SLOP = 3;     // CSS px around a mark that still counts as hovering it

  // Mark kinds: kind -> colour token (the one the chart or the row badges use, so there is no legend)
  // and the tooltip label (a string, or a function of the row). `segment` is the order of a kind
  // inside a merged mark; kinds that share a segment sit side by side. A new kind adds one entry
  // here and a case in markKind.
  const MARK_KINDS = {
    prompt: { token: "--accent", segment: 0, label: function (row) { return String(row.text || "").split("\n")[0]; } },
    compaction: { token: "--compact", segment: 1, label: "Compaction" },
    late_compact: { token: "--late-compact", segment: 1, label: "Late compact" },
    setting_change: { token: "--setting", segment: 2, label: "Setting change" },
    idle_gap: { token: "--warn", segment: 3, label: "Idle gap" },
    link: { token: "--muted", segment: 4,   // side session or fork, either end of the link
      label: function (row) {
        if (row.kind === "forked_from") { return "Forked from"; }
        return (row.link_kind === "fork" ? "Fork" : "Side session") + " started";
      } },
    cold: { token: "--up", segment: 5,       // the chart's cold colour; the badge's --warn is the idle gap's
      label: function (row) { return row.cold_resume ? "Cold resume" : "Cold write"; } },
    advisor: { token: "--advisor", segment: 6, label: "Advisor call" },  // no chart or badge colour exists for it
  };
  // The kind a row is marked as, or null: plain requests stay unmarked.
  function markKind(row) {
    switch (row.kind) {
      case "prompt": return "prompt";
      case "compaction": return row.late ? "late_compact" : "compaction";
      case "setting_change": return "setting_change";
      case "idle_gap": return "idle_gap";
      case "child_session": case "forked_from": return "link";
      case "request": return row.cold_write ? "cold" : null;
      case "advisor_call": return "advisor";
      default: return null;
    }
  }
  function markLabel(m) {
    const label = MARK_KINDS[m.kind].label;
    return typeof label === "function" ? label(m.row) : label;
  }

  let marks = [];         // {index, kind, row, top, bottom} for the rows that have a mark, in row order
  let groups = [];        // marks whose extents overlap: {top, bottom, marks (by segment), kinds (distinct, by segment)}
  let count = 0;          // rows in the list
  let dragging = false;
  let width = 0;
  let height = 0;
  let observedSpace = null;
  let spaceObserver = null;
  const layer = document.createElement("canvas");  // the marks and the search strip, drawn only when they change
  let searchMarks = [];   // row indexes of the search matches

  const token = window.theme.token;
  function yOf(index) { return count ? index / count * height : 0; }

  // Marks whose drawn extents overlap (the minimum height included) merge into one group, its kinds in segment order.
  function regroup() {
    groups = [];
    marks.forEach(function (m) {
      m.top = yOf(m.index);
      m.bottom = Math.max(m.top + MIN_MARK, yOf(m.index + 1));
      const last = groups[groups.length - 1];
      if (last && m.top < last.bottom) {
        last.marks.push(m);
        last.bottom = Math.max(last.bottom, m.bottom);
      } else {
        groups.push({ top: m.top, bottom: m.bottom, marks: [m] });
      }
    });
    groups.forEach(function (g) {
      g.marks.sort(function (a, b) { return MARK_KINDS[a.kind].segment - MARK_KINDS[b.kind].segment; });  // stable
      g.kinds = [];
      g.marks.forEach(function (m) { if (g.kinds.indexOf(m.kind) < 0) { g.kinds.push(m.kind); } });
    });
  }

  // One colour segment per kind, side by side across the strip.
  function drawMarks(ctx) {
    groups.forEach(function (g) {
      const w = width / g.kinds.length;
      g.kinds.forEach(function (kind, i) {
        ctx.fillStyle = token(MARK_KINDS[kind].token);
        ctx.fillRect(i * w, g.top, w, g.bottom - g.top);
      });
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

  // A match inside a collapsed Subagent block sits at the block's index, since it has no row of its own.
  function resolveMatches() {
    const rows = api.getRows();
    const at = new Map();
    function note(list, index) { list.forEach(function (r) { at.set(r, index); if (r.rows) { note(r.rows, index); } }); }
    rows.forEach(function (r, i) { at.set(r, i); if (r.kind === "subagent" && !r.expanded) { note(r.rows || [], i); } });
    const seen = new Set();
    api.getMatches().forEach(function (m) { if (at.has(m.row)) { seen.add(at.get(m.row)); } });
    searchMarks = Array.from(seen);
  }

  // The search strip, right of the marks.
  function drawSearch(ctx) {
    ctx.fillStyle = token("--search-match");
    searchMarks.forEach(function (index) {
      const y = yOf(index);
      ctx.fillRect(width, y, SEARCH_W, Math.max(MIN_MARK, yOf(index + 1) - y));
    });
  }

  // The marks and the search strip go to the offscreen layer; they only change with the rows, the
  // search, the size or the theme.
  function paintLayer() {
    const dpr = window.devicePixelRatio || 1;
    const ctx = layer.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width + SEARCH_W, height);
    drawMarks(ctx);
    drawSearch(ctx);
  }

  // The visible canvas: the layer, then the box. A scroll only redraws this.
  function draw() {
    if (canvas.hidden || !width || !height) { return; }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(layer, 0, 0);
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawView(ctx);
  }

  function paint() {
    if (canvas.hidden || !width || !height) { return; }
    paintLayer();
    draw();
  }

  function onSearch() { resolveMatches(); paint(); }

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    width = canvas.clientWidth - SEARCH_W;
    height = canvas.clientHeight;
    canvas.width = layer.width = Math.round(canvas.clientWidth * dpr);
    canvas.height = layer.height = Math.round(height * dpr);
    regroup();
    paint();
  }

  // The header ends where the rows end: the map's width plus the rows' own scrollbar, when it takes space.
  function setGutter() {
    const scrollbar = api.body.offsetWidth - api.body.clientWidth;
    box.style.setProperty("--tl-gutter", (canvas.offsetWidth + scrollbar) + "px");
  }

  // The map is hidden while the whole Timeline fits in its box without scrolling.
  function updateFit() {
    const wholeFits = api.body.scrollHeight <= api.body.clientHeight;
    if (!canvas.hidden && !wholeFits) { setGutter(); }  // a scrollbar may have appeared or gone
    if (canvas.hidden === wholeFits) { return; }
    canvas.hidden = wholeFits;
    box.classList.toggle("has-map", !wholeFits);
    if (!wholeFits) { setGutter(); resize(); }
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
    regroup();
    resolveMatches();
    const space = api.body.firstElementChild;
    if (space && space !== observedSpace && window.ResizeObserver) {  // rows re-measured: the fit can change
      observedSpace = space;
      if (spaceObserver) { spaceObserver.disconnect(); }
      spaceObserver = new ResizeObserver(updateFit);
      spaceObserver.observe(space);
    }
    updateFit();
    paint();
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
    groups.forEach(function (g) {
      const dist = y < g.top ? g.top - y : (y > g.bottom ? y - g.bottom : 0);
      if (dist < bestDist) { best = g; bestDist = dist; }
    });
    return best;
  }
  // The marked row nearest to the pointer within a merged mark (a tie goes to the earlier segment, prompts first).
  function nearestMark(g, y) {
    let best = g.marks[0];
    let bestDist = Infinity;
    g.marks.forEach(function (m) {
      const dist = y < m.top ? m.top - y : (y > m.bottom ? y - m.bottom : 0);
      if (dist < bestDist) { best = m; bestDist = dist; }
    });
    return best;
  }
  function tipText(g, y) {
    const more = g.marks.length - 1;
    return markLabel(nearestMark(g, y)) + (more ? " +" + more + " more" : "");
  }
  function showTip(event) {
    const g = dragging ? null : markAt(event);
    if (!g) { tip.hidden = true; return; }
    tip.textContent = tipText(g, event.clientY - canvas.getBoundingClientRect().top);
    tip.style.top = (event.clientY - tip.parentNode.getBoundingClientRect().top) + "px";
    tip.hidden = false;
  }

  canvas.addEventListener("pointerdown", function (event) {
    // Only a plain primary press scrubs: a right-click or a macOS ctrl-click opens the context menu,
    // which swallows the pointerup.
    if (event.button !== 0 || (event.ctrlKey && /Mac/.test(navigator.platform))) { return; }
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
  canvas.addEventListener("lostpointercapture", function () { dragging = false; });
  canvas.addEventListener("contextmenu", function () { dragging = false; });
  window.addEventListener("blur", function () { dragging = false; });
  canvas.addEventListener("pointerleave", function () { tip.hidden = true; });

  api.onRowsChange(rebuild);
  api.onSearchChange(onSearch);
  api.onVisibleRangeChange(function () { updateFit(); draw(); });
  if (window.ResizeObserver) {
    new ResizeObserver(function () { updateFit(); if (!canvas.hidden) { resize(); } }).observe(api.body);
  }
  if (window.matchMedia) {  // the tokens change with the theme; there is no other theme signal
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", paint);
  }
  // The backing size follows the device pixel ratio too (another display, browser zoom): watch the
  // current ratio and re-arm for the new one each time it changes.
  function watchDpr() {
    if (!window.matchMedia) { return; }
    const query = window.matchMedia("(resolution: " + (window.devicePixelRatio || 1) + "dppx)");
    query.addEventListener("change", function () { if (!canvas.hidden) { resize(); } watchDpr(); }, { once: true });
  }
  watchDpr();
  if (api.getRows().length) { rebuild(); }
}
