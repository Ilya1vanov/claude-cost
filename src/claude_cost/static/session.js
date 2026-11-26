document.querySelectorAll(".copy-id").forEach(function (button) {
  button.addEventListener("click", function () {
    navigator.clipboard.writeText(button.dataset.copy).then(function () {
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = "Copy"; }, 1500);
    });
  });
});

(function () {
  var d = JSON.parse(document.getElementById("chart-data").textContent);
  function marked(list, color) {
    return d.context.map(function (_, i) { return list.indexOf(i) >= 0 ? color : "rgba(54,162,235,0.8)"; });
  }
  var line = theme.token("--text"), cold = theme.token("--up"), idle = theme.token("--warn"),
      late = theme.token("--late"), mark = theme.token("--compact"), lateMark = theme.token("--late-compact");
  // Vertical line at each given point, in the given colour and dash pattern
  function verticalLines(id, list, color, dash, pixel) {
    return { id: id, afterDatasetsDraw: function (chart) {
      var a = chart.chartArea, ctx = chart.ctx;
      list().forEach(function (c) {
        var px = pixel(chart.scales.x, c);
        ctx.save(); ctx.strokeStyle = color(c); ctx.lineWidth = 2; ctx.setLineDash(dash);
        ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke(); ctx.restore();
      });
    } };
  }
  // At every compaction, between the requests before and after it (solid)
  var compactionLines = verticalLines("compactionLines", function () { return d.compactions; },
    function (c) { return c.late ? lateMark : mark; }, [], function (x, c) {
      var left = x.getPixelForValue(Math.max(c.index - 1, 0)), right = x.getPixelForValue(Math.min(c.index, d.labels.length - 1));
      return c.index === 0 ? left : c.index >= d.labels.length ? right : (left + right) / 2;
    });
  // At every Setting change, in its own colour (dashed)
  var settingColor = theme.token("--setting");
  var settingLines = verticalLines("settingLines", function () { return d.settingChanges; },
    function () { return settingColor; }, [5, 4], function (x, c) {
      return (x.getPixelForValue(c.index - 1) + x.getPixelForValue(c.index)) / 2;
    });
  function changeAt(index) {
    return d.settingChanges.filter(function (c) { return c.index === index; })[0];
  }
  // Labels are local "YYYY-MM-DD HH:MM:SS". A tick shows the date when it is the first tick or
  // its day differs from the previous tick's; otherwise time only. The tooltip shows both.
  function tickLabel(value, index, ticks) {
    var label = this.getLabelForValue(value);
    var prev = index > 0 ? this.getLabelForValue(ticks[index - 1].value) : null;
    return prev === null || prev.slice(0, 10) !== label.slice(0, 10) ? label : label.slice(11);
  }
  new Chart(document.getElementById("timeline"), {
    plugins: [compactionLines, settingLines],
    data: {
      labels: d.labels,
      datasets: [
        { type: "line", label: "Context (tokens)", data: d.context,
          yAxisID: "y", borderColor: line, borderWidth: 1.5, pointBorderWidth: 0,
          pointBackgroundColor: d.context.map(function (_, i) {
            return d.cold.indexOf(i) >= 0 ? cold : d.idle.indexOf(i) >= 0 ? idle :
              d.over.indexOf(i) >= 0 ? late : line; }),
          pointRadius: d.context.map(function (_, i) {
            return d.cold.indexOf(i) >= 0 || d.idle.indexOf(i) >= 0 || d.over.indexOf(i) >= 0 ? 5 : 2; }) },
        { type: "line", label: "Compact threshold", data: d.threshold, yAxisID: "y",
          borderColor: late, borderDash: [6, 4], pointRadius: 0, stepped: true },
        { type: "bar", label: "Cost ($)", data: d.cost, yAxisID: "y1", backgroundColor: theme.token("--bar-soft") },
      ],
    },
    options: { plugins: { tooltip: { callbacks: { title: function (items) { return items[0].label; },
        afterTitle: function (items) { var c = changeAt(items[0].dataIndex); return c ? "Setting change: " + c.label : ""; } } },
        legend: { display: false }, subtitle: { display: d.late.length > 0, color: lateMark,
        text: "Late compact from " + d.late.map(function (l) { return l.pre.toLocaleString(); }).join(", ") + " tokens" } },
      scales: { x: { ticks: { callback: tickLabel } }, y: { position: "left" }, y1: { position: "right", grid: { drawOnChartArea: false } } } },
  });
})();

// Timeline: rows come from the JSON endpoint in one request. A row is a plain object tagged by
// `kind`; ROW_RENDERERS has one function per kind, each returning that row's DOM element.
// renderRow(row) is the single entry point for drawing one row; timeline_window.js calls it for
// just the rows near the scroll position.
(function () {
  var box = document.getElementById("timeline-box");
  if (!box) { return; }
  var body = document.getElementById("timeline-rows");
  var COLLAPSED_LINES = 4;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }

  // Collapse text: a global flag (a class on the box, saved in localStorage) plus per-row overrides.
  // Both live here, not in the DOM, because windowing redraws a row each time it scrolls into view.
  var STORAGE_KEY = "timeline-collapse-text";
  var expanded = new WeakSet();  // row objects the user opened while the flag is on

  function readSaved() {
    try { return window.localStorage.getItem(STORAGE_KEY) === "1"; } catch (e) { return false; }
  }

  function setRowExpanded(row, on) {
    if (on) { expanded.add(row); } else { expanded.delete(row); }
  }

  // Makes a text row (prompt or request) open on click while collapsing is on.
  function collapsible(wrap, row) {
    wrap.classList.toggle("expanded", expanded.has(row));
    wrap.addEventListener("click", function (ev) {
      if (!box.classList.contains("collapse-text") || ev.target.closest("button")) { return; }
      setRowExpanded(row, !expanded.has(row));
      wrap.classList.toggle("expanded", expanded.has(row));
    });
    return wrap;
  }

  function setCollapsed(on) {
    var api = window.sessionTimeline;
    var first = api.getFirstVisibleIndex ? api.getFirstVisibleIndex() : -1;
    box.classList.toggle("collapse-text", on);
    try { window.localStorage.setItem(STORAGE_KEY, on ? "1" : "0"); } catch (e) { /* storage unavailable */ }
    if (first >= 0 && api.scrollToIndex) {  // rows resize after layout: hold the first row in view
      requestAnimationFrame(function () { api.scrollToIndex(first, { align: "start" }); });
    }
  }

  var toggle = document.getElementById("timeline-collapse");
  if (toggle) {
    var saved = readSaved();
    toggle.checked = saved;
    box.classList.toggle("collapse-text", saved);
    toggle.addEventListener("change", function () { setCollapsed(toggle.checked); });
  }

  function renderPrompt(row) {
    var wrap = el("div", "tl-row tl-prompt");
    wrap.appendChild(el("div", "tl-bubble" + (row.command ? " command" : ""), row.text));
    return collapsible(wrap, row);
  }

  function renderText(text) {
    var p = el("div", "tl-text", text);
    var wrap = el("div");
    wrap.appendChild(p);
    if (text.split("\n").length > COLLAPSED_LINES || text.length > COLLAPSED_LINES * 90) {
      p.classList.add("clamped");
      var more = el("button", "ghost small", "Show more");
      more.type = "button";
      more.addEventListener("click", function () {
        var open = p.classList.toggle("clamped");
        more.textContent = open ? "Show more" : "Show less";
      });
      wrap.appendChild(more);
    }
    return wrap;
  }

  // One list of a request's flag labels, used both to draw the badges and to search them
  var LATE_COMPACT_LABEL = "Late compact";
  function requestBadgeList(row) {
    var list = [];
    if (row.cold_write) { list.push({ cls: "cold", label: "Cold write" }); }
    if (row.cold_resume) { list.push({ cls: "cold", label: "Cold resume" }); }
    if (row.oversized) { list.push({ cls: "over", label: "Oversized context (above " + row.threshold.toLocaleString() + ")" }); }
    return list;
  }
  function requestBadges(row) {
    var badges = el("div", "tl-badges");
    requestBadgeList(row).forEach(function (b) { badges.appendChild(el("span", "badge " + b.cls, b.label)); });
    return badges;
  }

  // A tool line is one ellipsized line; a click shows its whole argument. Kept per tool object,
  // like `expanded`, so it survives windowing redraws.
  var openTools = new WeakSet();
  function renderTool(t) {
    var line = el("div", "tl-tool");
    line.appendChild(el("b", null, t.name));
    var arg = document.createTextNode("");
    line.appendChild(arg);
    function draw() {
      var open = openTools.has(t);
      line.classList.toggle("open", open);
      arg.textContent = (open ? t.full : t.summary) ? " " + (open ? t.full : t.summary) : "";
    }
    draw();
    if (t.full) {
      line.title = "Click to show or hide the whole call";
      line.addEventListener("click", function (ev) {
        ev.stopPropagation();  // not a click on the row, which toggles Collapse text
        if (openTools.has(t)) { openTools.delete(t); } else { openTools.add(t); }
        draw();
      });
    }
    return line;
  }

  function renderRequest(row) {
    var wrap = el("div", "tl-row tl-request" + (row.oversized ? " oversized" : ""));
    var main = el("div");
    main.appendChild(el("div", "tl-time", row.time));
    var badges = requestBadges(row);
    if (badges.childNodes.length) { main.appendChild(badges); }
    if (!row.content_available) {
      main.appendChild(el("div", "tl-gone", "transcript no longer on disk"));
    } else {
      if (row.text) { main.appendChild(renderText(row.text)); }
      row.tools.forEach(function (t) { main.appendChild(renderTool(t)); });
    }
    wrap.appendChild(main);
    wrap.appendChild(el("div", "num ctx", row.context_tokens.toLocaleString()));
    wrap.appendChild(el("div", "num", "$" + row.cost.toFixed(4)));
    return collapsible(wrap, row);
  }

  // An Advisor call: the model and cost it was billed, nothing else (it is not part of the Stream),
  // so the context column stays empty.
  function renderAdvisorCall(row) {
    var wrap = el("div", "tl-row tl-request tl-advisor");
    var main = el("div");
    main.appendChild(el("div", "tl-time", row.time));
    var badges = el("div", "tl-badges");
    badges.appendChild(el("span", "badge", "Advisor call"));
    badges.appendChild(el("span", "badge model-" + (row.modelFamily || "unknown"), row.modelLabel));
    main.appendChild(badges);
    wrap.appendChild(main);
    wrap.appendChild(el("div", "num ctx"));
    wrap.appendChild(el("div", "num", "$" + row.cost.toFixed(4)));
    return wrap;
  }

  // Events are dividers across the full width: a time, a label and a detail
  function divider(row, cls, label, detail) {
    var wrap = el("div", "tl-row tl-event " + cls);
    wrap.appendChild(el("span", "tl-time", row.time));
    wrap.appendChild(el("span", "tl-event-label", label));
    if (detail) { wrap.appendChild(detail); }
    return wrap;
  }

  function sessionLink(id, label) {
    var a = el("a", null, label);
    a.href = "/session/" + encodeURIComponent(id);
    return a;
  }

  function span(children) {
    var s = el("span", "tl-event-detail");
    children.forEach(function (c) { s.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return s;
  }

  // The label an event divider draws (also what the search matches)
  var EVENT_LABELS = { setting_change: "Setting change", compaction: "Compaction", idle_gap: "Idle gap",
    forked_from: "Forked from" };
  function eventLabel(row) {
    if (row.kind === "child_session") { return (row.link_kind === "fork" ? "Fork" : "Side session") + " started"; }
    return EVENT_LABELS[row.kind];
  }

  function renderSettingChange(row) {
    return divider(row, "setting-change", eventLabel(row), span([row.label]));
  }

  function renderCompaction(row) {
    var detail = span([row.pre_tokens.toLocaleString() + " to " + row.post_tokens.toLocaleString() +
      " tokens (" + row.trigger + ") "]);
    if (row.late) { detail.appendChild(el("span", "badge late-compact", LATE_COMPACT_LABEL)); }
    var wrap = divider(row, "compaction" + (row.late ? " late-compact" : ""), eventLabel(row), detail);
    if (row.summary) {
      var box = el("details", "tl-summary");
      box.appendChild(el("summary", null, "Summary"));
      box.appendChild(el("div", "tl-text", row.summary));
      wrap.appendChild(box);
    }
    return wrap;
  }

  function renderIdleGap(row) {
    var minutes = Math.round(row.gap_seconds / 60);
    return divider(row, "idle-gap", eventLabel(row), span([minutes + " min before the next request"]));
  }

  function renderChildSession(row) {
    return divider(row, "child-session", eventLabel(row), span([sessionLink(row.session_id, row.label),
      ": $" + row.cost.toFixed(2) + ", " + row.requests + " requests"]));
  }

  function renderForkedFrom(row) {
    return divider(row, "forked-from", eventLabel(row), span([sessionLink(row.parent_session_id, row.parent_label),
      " at " + row.time]));
  }

  // The model and Effort badges for a setting; an Effort this stylesheet has no color for stays plain.
  var EFFORTS = ["low", "medium", "high", "xhigh", "max"];
  function settingBadges(setting, into) {
    into.appendChild(el("span", "badge model-" + (setting.modelFamily || "unknown"), setting.modelLabel));
    if (setting.effort) {
      into.appendChild(document.createTextNode(" "));
      into.appendChild(el("span", "badge" + (EFFORTS.indexOf(setting.effort) >= 0 ? " effort-" + setting.effort : ""), setting.effort));
    }
  }

  // A Subagent block: collapsed by default. Its rows are not drawn inside it; expanding it adds
  // them to the flat windowed list right after it, one level deeper (see flatten below).
  function renderSubagent(row) {
    var wrap = el("div", "tl-row tl-request tl-subagent");
    var main = el("div");
    var head = el("button", "ghost small tl-toggle", (row.expanded ? "Collapse " : "Expand ") + (row.agent_type || row.agent_id));
    head.type = "button";
    head.setAttribute("aria-expanded", row.expanded ? "true" : "false");
    head.addEventListener("click", function () { toggleSubagent(row.agent_id); });
    main.appendChild(head);
    if (row.description) { main.appendChild(el("span", "tl-text", " " + row.description)); }
    var meta = el("div", "tl-time", row.time + " · " + (row.is_fork ? "Fork subagent · " : "") + "\u00a0");
    settingBadges(row, meta);
    meta.appendChild(document.createTextNode(" · " + row.requests + " requests"));
    main.appendChild(meta);
    wrap.appendChild(main);
    wrap.appendChild(el("div", "num ctx", row.peak_context.toLocaleString() + " peak"));
    wrap.appendChild(el("div", "num", "$" + row.cost.toFixed(4)));
    return wrap;
  }

  var ROW_RENDERERS = { prompt: renderPrompt, request: renderRequest, advisor_call: renderAdvisorCall, subagent: renderSubagent,
    setting_change: renderSettingChange, compaction: renderCompaction, idle_gap: renderIdleGap,
    child_session: renderChildSession, forked_from: renderForkedFrom };

  function renderRow(row) {
    var render = ROW_RENDERERS[row.kind];
    var node = render ? render(row) : null;  // a kind this script does not know is not drawn
    if (node && row === hitRow) { node.classList.add("tl-hit"); }  // search highlight lives in JS state
    if (node && row.depth) { node.style.marginLeft = (row.depth * 24) + "px"; }
    return node;
  }

  // The rows arrive as a tree (a subagent row holds its own rows). The windowed list is the
  // flattening of that tree under the open blocks: each entry is the row itself with `depth`
  // and `expanded`, children directly after their block.
  var tree = [];
  var openAgents = new Set();  // agent ids of the Subagent blocks that are open
  var flat = [];
  function flatten(rows, depth, out) {
    rows.forEach(function (row) {
      var isOpen = row.kind === "subagent" && openAgents.has(row.agent_id);
      row.depth = depth;  // the row objects stay the same across refreshes (Collapse text keys on them)
      row.expanded = isOpen;
      out.push(row);
      if (isOpen) { flatten(row.rows, depth + 1, out); }
    });
    return out;
  }
  function findPath(rows, agentId) {  // agent ids from the outermost block down to `agentId`
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].kind !== "subagent") { continue; }
      if (rows[i].agent_id === agentId) { return [agentId]; }
      var inner = findPath(rows[i].rows, agentId);
      if (inner) { return [rows[i].agent_id].concat(inner); }
    }
    return null;
  }
  function refresh(first) {
    flat = flatten(tree, 0, []);
    if (first) { window.sessionTimeline.mountRows(flat); } else { window.sessionTimeline.updateRows(flat); }
  }
  function indexOfAgent(agentId) {
    return flat.findIndex(function (r) { return r.kind === "subagent" && r.agent_id === agentId; });
  }
  function toggleSubagent(agentId) {
    if (openAgents.has(agentId)) { openAgents.delete(agentId); } else { openAgents.add(agentId); }
    refresh(false);
  }
  // Expands the block of `agentId` (and every block around it); returns the index of its row in
  // the windowed list, or -1 when no such Subagent is in this Timeline. Child rows follow it, so
  // scrollToIndex(index + n) reaches the n-th row inside.
  function expandSubagent(agentId) {
    var path = findPath(tree, agentId);
    if (!path) { return -1; }
    path.forEach(function (id) { openAgents.add(id); });
    refresh(false);
    return indexOfAgent(agentId);
  }
  function collapseSubagent(agentId) {
    openAgents.delete(agentId);
    refresh(false);
  }
  // The model and Effort in force at windowed row `index`: a request has its own; any other row, a
  // Subagent block included (it sits in its parent's stream), takes the next request at its depth,
  // else the previous one.
  function settingAt(index) {
    var row = flat[index];
    if (!row) { return null; }
    function request(i) { return flat[i].depth === row.depth && flat[i].kind === "request"; }
    var near = row.kind === "request" ? row : null;
    for (var i = index + 1; !near && i < flat.length && flat[i].depth >= row.depth; i++) {
      if (request(i)) { near = flat[i]; }
    }
    for (var j = index - 1; !near && j >= 0; j--) {
      if (request(j)) { near = flat[j]; }
    }
    return near && { model: near.model, modelLabel: near.modelLabel, modelFamily: near.modelFamily, effort: near.effort };
  }
  function setTree(rows) { tree = rows; openAgents.clear(); hitRow = null; matches = []; refresh(true); notifySearch(); }

  // Search: over the whole row tree (not the DOM), so rows that are not drawn and rows inside
  // collapsed Subagent blocks count. Each match is {row, agentId} (agentId: the innermost block
  // holding it, null at the top level). The current match is kept in `hitRow`, applied by renderRow.
  var matches = [];
  var current = -1;
  var hitRow = null;
  var searchListeners = [];  // told when a search runs or clears (the Timeline map draws the matches)
  function notifySearch() { searchListeners.slice().forEach(function (fn) { fn(); }); }
  function searchText(row) {
    var parts = [];
    if (row.kind === "prompt") { parts.push(row.text); }
    if (row.kind === "request") {
      parts.push(row.text);
      requestBadgeList(row).forEach(function (b) { parts.push(b.label); });
      if (row.content_available) { row.tools.forEach(function (t) { parts.push(t.name, t.summary); }); }
    } else if (row.kind === "advisor_call") {
      parts.push("Advisor call", row.modelLabel);
    } else if (row.kind === "subagent") {
      parts.push(row.agent_type, row.agent_id, row.description, row.modelLabel);
    } else {
      parts.push(eventLabel(row), row.label, row.parent_label, row.trigger, row.summary);
      if (row.late) { parts.push(LATE_COMPACT_LABEL); }
    }
    return parts.filter(Boolean).join("\n").toLowerCase();
  }
  function findMatches(rows, needle, agentId, out) {
    rows.forEach(function (row) {
      if (searchText(row).indexOf(needle) >= 0) { out.push({ row: row, agentId: agentId }); }
      if (row.kind === "subagent") { findMatches(row.rows, needle, row.agent_id, out); }
    });
    return out;
  }
  function markHit(index) {  // moves the highlight on the drawn rows; renderRow handles later redraws
    var old = body.querySelector(".tl-hit");
    if (old) { old.classList.remove("tl-hit"); }
    var item = body.querySelector('[data-index="' + index + '"]');
    if (item && item.firstChild) { item.firstChild.classList.add("tl-hit"); }
  }
  function showMatch(n) {
    var m = matches[n];
    current = n;
    hitRow = m.row;
    if (m.agentId && !flat.includes(m.row)) { expandSubagent(m.agentId); }  // reveal a match inside a collapsed block
    var index = flat.indexOf(m.row);
    if (index < 0) { return; }
    if (box.classList.contains("collapse-text") && (m.row.kind === "prompt" || m.row.kind === "request")) {
      window.sessionTimeline.expandText(index, true);
    }
    markHit(index);
    window.sessionTimeline.scrollToIndex(index, { align: "center" });
    updateCount();
  }
  var searchBox = document.getElementById("timeline-search");
  var searchCount = document.getElementById("timeline-search-count");
  function updateCount() {
    if (searchCount) { searchCount.textContent = searchBox.value ? (matches.length ? (current + 1) + " / " + matches.length : "0 / 0") : ""; }
  }
  function runSearch() {
    var needle = searchBox.value.trim().toLowerCase();
    matches = needle ? findMatches(tree, needle, null, []) : [];
    current = -1;
    hitRow = null;
    markHit(-1);
    if (matches.length) { showMatch(0); } else { updateCount(); }
    notifySearch();
  }
  function step(delta) {
    if (matches.length) { showMatch((current + delta + matches.length) % matches.length); }
  }
  if (searchBox) {
    searchBox.addEventListener("input", runSearch);
    searchBox.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowDown" || (ev.key === "Enter" && !ev.shiftKey)) { ev.preventDefault(); step(1); }
      else if (ev.key === "ArrowUp" || (ev.key === "Enter" && ev.shiftKey)) { ev.preventDefault(); step(-1); }
    });
  }

  function loadRows(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    }).then(function (data) { return data.rows; });
  }

  // timeline_window.js (a module script placed after this one) windows these rows into view
  // and adds mountRows, updateRows, scrollToIndex, remeasure and onFirstVisibleChange to this object.
  window.sessionTimeline = { ROW_RENDERERS: ROW_RENDERERS, renderRow: renderRow, loadRows: loadRows, body: body, el: el,
    setRowExpanded: setRowExpanded, settingBadges: settingBadges,
    setTree: setTree, expandSubagent: expandSubagent, collapseSubagent: collapseSubagent,
    rowAt: function (i) { return flat[i]; }, settingAt: settingAt,
    getMatches: function () { return matches; },
    onSearchChange: function (fn) {
      searchListeners.push(fn);
      return function () { var i = searchListeners.indexOf(fn); if (i >= 0) { searchListeners.splice(i, 1); } };
    } };
})();
