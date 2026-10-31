(function () {
  var d = JSON.parse(document.getElementById("chart-data").textContent);
  function marked(list, color) {
    return d.context.map(function (_, i) { return list.indexOf(i) >= 0 ? color : "rgba(54,162,235,0.8)"; });
  }
  var line = theme.token("--text"), cold = theme.token("--up"), idle = theme.token("--warn"),
      late = theme.token("--late"), mark = theme.token("--compact"), lateMark = theme.token("--late-compact");
  // Vertical line at every compaction, between the requests before and after it
  var compactionLines = { id: "compactionLines", afterDatasetsDraw: function (chart) {
    var x = chart.scales.x, a = chart.chartArea, ctx = chart.ctx;
    d.compactions.forEach(function (c) {
      var left = x.getPixelForValue(Math.max(c.index - 1, 0)), right = x.getPixelForValue(Math.min(c.index, d.labels.length - 1));
      var px = c.index === 0 ? left : c.index >= d.labels.length ? right : (left + right) / 2;
      ctx.save(); ctx.strokeStyle = c.late ? lateMark : mark; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke(); ctx.restore();
    });
  } };
  new Chart(document.getElementById("timeline"), {
    plugins: [compactionLines],
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
    options: { plugins: { legend: { display: false }, subtitle: { display: d.late.length > 0, color: lateMark,
        text: "Late compact from " + d.late.map(function (l) { return l.pre.toLocaleString(); }).join(", ") + " tokens" } },
      scales: { y: { position: "left" }, y1: { position: "right", grid: { drawOnChartArea: false } } } },
  });
})();
