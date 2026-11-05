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

  function renderPrompt(row) {
    var wrap = el("div", "tl-row tl-prompt");
    wrap.appendChild(el("div", "tl-bubble", row.text));
    return wrap;
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
    return wrap;
  }

  var ROW_RENDERERS = { prompt: renderPrompt, request: renderRequest };

  function renderRow(row) {
    var render = ROW_RENDERERS[row.kind];
    return render ? render(row) : null;  // a kind this script does not know is not drawn
  }

  function loadRows(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    }).then(function (data) { return data.rows; });
  }

  // timeline_window.js (a module script, so it runs after this one) windows these rows into view
  // and adds scrollToIndex, remeasure and onFirstVisibleChange to this object.
  window.sessionTimeline = { ROW_RENDERERS: ROW_RENDERERS, renderRow: renderRow, loadRows: loadRows, body: body, el: el };
})();
