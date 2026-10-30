(function () {
  var data = JSON.parse(document.getElementById("chart-data").textContent);
  new Chart(document.getElementById("spotlight-chart"), {
    type: "bar",
    data: {
      labels: data.labels,
      datasets: data.series.map(function (s) {
        return { label: s.label, data: s.data, backgroundColor: theme.component(s.label) }; }),
    },
    options: { scales: { x: { stacked: true }, y: { stacked: true, title: { display: true, text: "Cost ($)" } } } },
  });
})();
