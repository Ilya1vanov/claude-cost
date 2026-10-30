// Chart colors come from the CSS tokens in app.css, so charts match the page in light and dark.
(function () {
  var css = getComputedStyle(document.documentElement);
  function token(name) { return css.getPropertyValue(name).trim(); }
  Chart.defaults.color = token("--muted");
  Chart.defaults.borderColor = token("--border");
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.maintainAspectRatio = false;  // the .chart box sets the height
  Chart.defaults.plugins.legend.labels.boxWidth = 12;
  window.theme = {
    token: token,
    // Cost component name ("cache write (5m)") -> its color, or "" when it is not one
    component: function (name) {
      return token("--c-" + String(name).replace(/[^a-z0-9]+/g, "-").replace(/-$/, ""));
    },
    series: function (i) { return token("--s" + (i % 6 + 1)); },
  };
})();
