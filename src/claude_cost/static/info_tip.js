// One floating tooltip for every .info icon: shown on hover or keyboard focus, never clipped by a scrolling table.
(function () {
  var tip = document.createElement("div");
  tip.id = "info-tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.appendChild(tip);
  function show(el) {
    tip.textContent = el.getAttribute("data-tip");
    tip.style.left = "0px";  // measure from a fixed spot, so the old position can't shrink the box
    tip.style.top = "0px";
    tip.hidden = false;
    var r = el.getBoundingClientRect(), t = tip.getBoundingClientRect();
    var left = Math.max(8, Math.min(r.left + r.width / 2 - t.width / 2, window.innerWidth - t.width - 8));
    var top = r.bottom + 6 + t.height > window.innerHeight ? r.top - t.height - 6 : r.bottom + 6;
    tip.style.left = left + "px";
    tip.style.top = Math.max(8, top) + "px";
  }
  function hide() { tip.hidden = true; }
  function icon(e) { return e.target.closest && e.target.closest(".info"); }
  function focused(el) { return el === document.activeElement; }
  document.addEventListener("mouseover", function (e) { var el = icon(e); if (el) show(el); });
  document.addEventListener("mouseout", function (e) { var el = icon(e); if (el && !focused(el)) hide(); });
  document.addEventListener("focusin", function (e) { var el = icon(e); if (el) show(el); });
  document.addEventListener("focusout", function (e) { if (icon(e)) hide(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
  document.addEventListener("scroll", hide, true);
})();
