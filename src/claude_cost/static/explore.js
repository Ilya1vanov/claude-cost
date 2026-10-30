(function () {
  var data = JSON.parse(document.getElementById("explore-data").textContent);
  new Chart(document.getElementById("explore-chart"), {
    type: "bar",
    data: { labels: data.labels, datasets: [{ label: "Cost ($)", data: data.costs }] },
    options: { scales: { x: { title: { display: true, text: data.label } },
                         y: { title: { display: true, text: "Cost ($)" } } } },
  });
})();
