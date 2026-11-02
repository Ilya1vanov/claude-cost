/* Period popover: a <details> that also closes on Esc, Cancel and a click outside it. */
(function () {
  var pop = document.getElementById("period-pop");
  if (!pop) return;
  function close() { pop.removeAttribute("open"); }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && pop.open) { close(); pop.querySelector("summary").focus(); }
  });
  document.addEventListener("click", function (e) {
    if (pop.open && !pop.contains(e.target)) close();
  });
  var cancel = pop.querySelector(".period-cancel");
  if (cancel) cancel.addEventListener("click", close);
})();
