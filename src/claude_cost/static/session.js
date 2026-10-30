(function () {
  var d = JSON.parse(document.getElementById("chart-data").textContent);
  function marked(list, color) {
    return d.context.map(function (_, i) { return list.indexOf(i) >= 0 ? color : "rgba(54,162,235,0.8)"; });
  }
  new Chart(document.getElementById("timeline"), {
    data: {
      labels: d.labels,
      datasets: [
        { type: "line", label: "Context (tokens); red = Cold write, orange = after idle gap", data: d.context,
          yAxisID: "y", borderColor: "#444",
          pointBackgroundColor: d.context.map(function (_, i) {
            return d.cold.indexOf(i) >= 0 ? "red" : d.idle.indexOf(i) >= 0 ? "orange" :
              d.over.indexOf(i) >= 0 ? "purple" : "#444"; }),
          pointRadius: d.context.map(function (_, i) {
            return d.cold.indexOf(i) >= 0 || d.idle.indexOf(i) >= 0 || d.over.indexOf(i) >= 0 ? 6 : 3; }) },
        { type: "line", label: "Compact threshold (purple = above it)", data: d.threshold, yAxisID: "y",
          borderColor: "purple", borderDash: [6, 4], pointRadius: 0, stepped: true },
        { type: "bar", label: "Cost ($)", data: d.cost, yAxisID: "y1" },
      ],
    },
    options: { plugins: { subtitle: { display: d.late.length > 0, color: "purple",
        text: "Late compact from " + d.late.map(function (l) { return l.pre.toLocaleString(); }).join(", ") + " tokens" } },
      scales: { y: { position: "left" }, y1: { position: "right", grid: { drawOnChartArea: false } } } },
  });
})();
