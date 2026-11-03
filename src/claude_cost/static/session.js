(function () {
  var d = JSON.parse(document.getElementById("chart-data").textContent);
  function marked(list, color) {
    return d.context.map(function (_, i) { return list.indexOf(i) >= 0 ? color : "rgba(54,162,235,0.8)"; });
  }
  var line = theme.token("--text"), cold = theme.token("--up"), idle = theme.token("--warn"),
      late = theme.token("--late"), mark = theme.token("--compact"), lateMark = theme.token("--late-compact");
  // Vertical line at every compaction, between the requests before and after it
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
