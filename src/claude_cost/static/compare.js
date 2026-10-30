(function () {
  var data = JSON.parse(document.getElementById("compare-data").textContent);
  new Chart(document.getElementById("compare-chart"), {
    type: "line",
    data: {
      labels: data.labels,
      datasets: data.series.map(function (s) { return { label: s.label, data: s.data }; }),
    },
    options: { scales: { x: { title: { display: true, text: "Day of period" } },
                         y: { title: { display: true, text: "Cost ($)" } } } },
  });
})();
