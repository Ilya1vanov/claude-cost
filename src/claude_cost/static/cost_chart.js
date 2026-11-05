(function () {
  var data = JSON.parse(document.getElementById("chart-data").textContent);
  new Chart(document.getElementById("cost-chart"), {
    type: "bar",
    data: { labels: data.labels, datasets: [{ label: "Cost ($)", data: data.costs, borderRadius: 3,
      backgroundColor: data.labels.map(function (l) {
        return (data.label === "Cost component" && theme.component(l)) || theme.token("--accent"); }) }] },
    options: { plugins: { legend: { display: false } },
               scales: { x: { title: { display: true, text: data.label } },
                         y: { title: { display: true, text: "Cost ($)" } } } },
  });
})();
