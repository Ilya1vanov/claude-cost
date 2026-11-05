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
    wrap.appendChild(el("div", "tl-bubble", row.text));
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

  function renderRequest(row) {
    var wrap = el("div", "tl-row tl-request" + (row.oversized ? " oversized" : ""));
    var main = el("div");
    main.appendChild(el("div", "tl-time", row.time));
    if (!row.content_available) {
      main.appendChild(el("div", "tl-gone", "transcript no longer on disk"));
    } else {
      if (row.text) { main.appendChild(renderText(row.text)); }
      row.tools.forEach(function (t) {
        var line = el("div", "tl-tool");
        line.appendChild(el("b", null, t.name));
        if (t.summary) { line.appendChild(document.createTextNode(" " + t.summary)); }
        main.appendChild(line);
      });
    }
    wrap.appendChild(main);
    wrap.appendChild(el("div", "num ctx", row.context_tokens.toLocaleString()));
    wrap.appendChild(el("div", "num", "$" + row.cost.toFixed(4)));
    return collapsible(wrap, row);
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
    main.appendChild(el("div", "tl-time", row.time + " · " + (row.is_fork ? "Fork subagent · " : "") + row.modelLabel +
      (row.effort ? " · " + row.effort : "") + " · " + row.requests + " requests"));
    wrap.appendChild(main);
    wrap.appendChild(el("div", "num ctx", row.peak_context.toLocaleString() + " peak"));
    wrap.appendChild(el("div", "num", "$" + row.cost.toFixed(4)));
    return wrap;
  }

  var ROW_RENDERERS = { prompt: renderPrompt, request: renderRequest, subagent: renderSubagent };

  function renderRow(row) {
    var render = ROW_RENDERERS[row.kind];
    var node = render ? render(row) : null;  // a kind this script does not know is not drawn
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
  // The model and Effort in force at windowed row `index`: a request or block has its own; a prompt
  // takes the next request at its depth, else the previous one.
  function settingAt(index) {
    var row = flat[index];
    if (!row) { return null; }
    if (row.model) { return { model: row.model, modelLabel: row.modelLabel, effort: row.effort }; }
    var near = null;
    for (var i = index + 1; i < flat.length && flat[i].depth >= row.depth; i++) {
      if (flat[i].depth === row.depth && flat[i].model) { near = flat[i]; break; }
    }
    for (var j = index - 1; !near && j >= 0; j--) {
      if (flat[j].depth === row.depth && flat[j].model) { near = flat[j]; }
    }
    return near && { model: near.model, modelLabel: near.modelLabel, effort: near.effort };
  }
  function setTree(rows) { tree = rows; openAgents.clear(); refresh(true); }

  function loadRows(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    }).then(function (data) { return data.rows; });
  }

  // timeline_window.js (a module script, so it runs after this one) windows these rows into view
  // and adds mountRows, updateRows, scrollToIndex, remeasure and onFirstVisibleChange to this object.
  window.sessionTimeline = { ROW_RENDERERS: ROW_RENDERERS, renderRow: renderRow, loadRows: loadRows, body: body, el: el,
    setRowExpanded: setRowExpanded,
    setTree: setTree, expandSubagent: expandSubagent, collapseSubagent: collapseSubagent,
    rowAt: function (i) { return flat[i]; }, settingAt: settingAt };
})();
