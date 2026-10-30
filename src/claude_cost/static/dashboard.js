(function () {
  var data = JSON.parse(document.getElementById("chart-data").textContent);
  new Chart(document.getElementById("daily-spend"), {
    type: "bar",
    data: {
      labels: data.map(function (d) { return d.day; }),
      datasets: [{ label: "Daily spend ($)", data: data.map(function (d) { return d.cost; }),
                   backgroundColor: theme.token("--accent"), borderRadius: 3 }],
    },
    options: { plugins: { legend: { display: false } } },
  });
})();
