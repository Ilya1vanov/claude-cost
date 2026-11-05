/* The whole Sessions row opens its Session view; the name stays a real link for keyboard and a11y. */
document.querySelectorAll("tr.row-link").forEach(function (row) {
  row.addEventListener("click", function (e) {
    if (e.target.closest("a")) return;
    window.location = row.dataset.href;
  });
});
