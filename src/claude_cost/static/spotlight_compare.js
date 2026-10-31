(function () {
  var MAX_LEGEND = 24;
  var data = JSON.parse(document.getElementById("compare-data").textContent);
  new Chart(document.getElementById("spotlight-compare-chart"), {
    type: "line",
    data: {
      labels: data.labels,
      datasets: data.series.map(function (s, i) {
        return { label: s.label, data: s.data, borderColor: theme.series(i), backgroundColor: theme.series(i) }; }),
    },
    options: { plugins: { tooltip: { callbacks: { label: function (item) {  // the full name, not the legend's shortened one
                 return item.dataset.label + ": $" + item.formattedValue; } } },
               legend: { labels: { generateLabels: function (chart) {
                 return Chart.defaults.plugins.legend.labels.generateLabels(chart).map(function (l) {
                   if (l.text.length > MAX_LEGEND) { l.text = l.text.slice(0, MAX_LEGEND - 1) + "\u2026"; }
                   return l; }); } } } },
               scales: { x: { title: { display: true, text: "Day since first activity" } },
                         y: { title: { display: true, text: "Cost ($)" } } } },
  });
})();
