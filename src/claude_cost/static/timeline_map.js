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

  // Mark kinds: kind -> colour token (the one the chart or the row badges use, so there is no legend).
  // `segment` is the order of a kind inside a merged mark; kinds that share a segment sit side by
  // side. A new kind adds one entry here and a case in markKind and markLabel.
  const MARK_KINDS = {
    prompt: { token: "--accent", segment: 0 },
    compaction: { token: "--compact", segment: 1 },
    late_compact: { token: "--late-compact", segment: 1 },
    setting_change: { token: "--setting", segment: 2 },
    idle_gap: { token: "--warn", segment: 3 },
    link: { token: "--muted", segment: 4 },   // side session or fork, either end of the link
    cold: { token: "--up", segment: 5 },      // the chart's cold colour; the badge's --warn is the idle gap's
    advisor: { token: "--advisor", segment: 6 },
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
    const row = m.row;
    switch (m.kind) {
      case "prompt": return String(row.text || "").split("\n")[0];
      case "compaction": return "Compaction";
      case "late_compact": return "Late compact";
      case "setting_change": return "Setting change";
      case "idle_gap": return "Idle gap";
      case "link":
        if (row.kind === "forked_from") { return "Forked from"; }
        return (row.link_kind === "fork" ? "Fork" : "Side session") + " started";
      case "cold": return row.cold_resume ? "Cold resume" : "Cold write";
      default: return "Advisor call";
    }
  }

  let marks = [];         // {index, kind, row} for the rows that have a mark, in row order
  let groups = [];        // marks on the same pixel: {top, bottom, marks (by segment), kinds (distinct, by segment)}
  let count = 0;          // rows in the list
  let fits = true;
  let dragging = false;
  let width = 0;
  let height = 0;
  let observedSpace = null;
  let searchMarks = [];   // row indexes of the search matches

  function token(name) { return window.theme ? window.theme.token(name) : getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function yOf(index) { return count ? index / count * height : 0; }

  // Marks that land on the same pixel merge into one group, its kinds in segment order.
  function regroup() {
    groups = [];
    let key = null;
    marks.forEach(function (m) {
      const top = yOf(m.index);
      const bottom = Math.max(top + MIN_MARK, yOf(m.index + 1));
      const last = groups[groups.length - 1];
      if (last && Math.floor(top) === key) {
        last.marks.push(m);
        last.bottom = Math.max(last.bottom, bottom);
      } else {
        key = Math.floor(top);
        groups.push({ top: top, bottom: bottom, marks: [m] });
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
  function resolveSearch() {
    const rows = api.getRows();
    const at = new Map();
    function note(list, index) { list.forEach(function (r) { at.set(r, index); if (r.rows) { note(r.rows, index); } }); }
    rows.forEach(function (r, i) { at.set(r, i); if (r.kind === "subagent" && !r.expanded) { note(r.rows || [], i); } });
    const seen = new Set();
    api.getMatches().forEach(function (m) { if (at.has(m.row)) { seen.add(at.get(m.row)); } });
    searchMarks = Array.from(seen);
    draw();
  }

  // The search strip, right of the marks.
  function drawSearch(ctx) {
    ctx.clearRect(width, 0, SEARCH_W, height);
    ctx.fillStyle = token("--search-match");
    searchMarks.forEach(function (index) {
      const y = yOf(index);
      ctx.fillRect(width, y, SEARCH_W, Math.max(MIN_MARK, yOf(index + 1) - y));
    });
  }

  function draw() {
    if (canvas.hidden || !width || !height) { return; }
    const dpr = window.devicePixelRatio || 1;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    drawMarks(ctx);
    drawSearch(ctx);
    drawView(ctx);
  }

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    width = canvas.clientWidth - SEARCH_W;
    height = canvas.clientHeight;
    canvas.width = Math.round(canvas.clientWidth * dpr);
    canvas.height = Math.round(height * dpr);
    regroup();
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
    regroup();
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
    groups.forEach(function (g) {
      const dist = y < g.top ? g.top - y : (y > g.bottom ? y - g.bottom : 0);
      if (dist < bestDist) { best = g; bestDist = dist; }
    });
    return best;
  }
  // The nearest marked row's label (the first of a merged mark: prompts, then the fixed order), and how many more share the spot.
  function tipText(g) {
    const more = g.marks.length - 1;
    return markLabel(g.marks[0]) + (more ? " +" + more + " more" : "");
  }
  function showTip(event) {
    const g = dragging ? null : markAt(event);
    if (!g) { tip.hidden = true; return; }
    tip.textContent = tipText(g);
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
  api.onRowsChange(resolveSearch);
  api.onSearchChange(resolveSearch);
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
