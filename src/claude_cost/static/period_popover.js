/* Period popover: a <details> that closes on Esc, Cancel and a click outside it, and holds a
   range calendar (range_calendar.js) for picking a Custom period. */
(function () {
  var pop = document.getElementById("period-pop");
  if (!pop) return;
  var cal = pop.querySelector(".period-cal");
  var apply = pop.querySelector(".period-apply");
  var cancel = pop.querySelector(".period-cancel");

  function close() { pop.removeAttribute("open"); }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && pop.open) { close(); pop.querySelector("summary").focus(); }
  });
  document.addEventListener("click", function (e) {
    // composedPath, not contains(): a paging click rebuilds the calendar, detaching its own target
    if (pop.open && e.composedPath().indexOf(pop) < 0) close();
  });
  if (cancel) cancel.addEventListener("click", close);
  if (!cal || !apply) return;

  var rc = RangeCalendar.mount(cal, {
    min: pop.dataset.min, max: pop.dataset.max, costsUrl: pop.dataset.costs,
    onChange: function (start, end) { apply.disabled = !(start && end); }
  });
  apply.addEventListener("click", function () {
    var r = rc.get();
    if (r.start && r.end) {
      location.href = pop.dataset.apply + "?period=custom&start=" + r.start + "&end=" + r.end;
    }
  });
  pop.addEventListener("toggle", function () {
    if (pop.open) { apply.hidden = false; rc.reset(pop.dataset.start, pop.dataset.end); }
  });
})();
