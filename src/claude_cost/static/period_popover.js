/* Period popover: a <details> that closes on Esc, Cancel and a click outside it, and holds a
   range calendar for picking a Custom period. Days are ISO strings (YYYY-MM-DD) throughout. */
(function () {
  var pop = document.getElementById("period-pop");
  if (!pop) return;
  var cal = pop.querySelector(".period-cal");
  var apply = pop.querySelector(".period-apply");
  var cancel = pop.querySelector(".period-cancel");
  var MIN = pop.dataset.min, MAX = pop.dataset.max;
  var narrow = window.matchMedia("(max-width: 640px)");
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December"];
  var WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

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

  /* ---- day arithmetic ---- */
  function parse(s) { var p = s.split("-"); return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])); }
  function iso(d) { return d.toISOString().slice(0, 10); }
  function addDays(s, n) { var d = parse(s); d.setUTCDate(d.getUTCDate() + n); return iso(d); }
  function monthStart(s) { return s.slice(0, 8) + "01"; }
  function addMonths(s, n) { var d = parse(monthStart(s)); d.setUTCMonth(d.getUTCMonth() + n); return iso(d); }
  function inRange(s) { return s >= MIN && s <= MAX; }
  function clampDay(s) { return s < MIN ? MIN : s > MAX ? MAX : s; }

  /* ---- state ---- */
  var start, end, hover, focus, first;   // first = first day of the leftmost shown month
  function shown() { return narrow.matches ? 1 : 2; }
  function lastFirst() { return addMonths(monthStart(MAX), -(shown() - 1)); }   // rightmost month may be today's
  function clampFirst(m) {
    var lo = monthStart(MIN), hi = lastFirst();
    return m < lo ? lo : m > hi ? hi : m;
  }

  function reset() {
    start = pop.dataset.start; end = pop.dataset.end; hover = null;
    focus = clampDay(end);
    first = clampFirst(addMonths(end, -(shown() - 1)));   // the end month is the rightmost shown
    build();
  }

  /* ---- rendering ---- */
  var grid;
  function build() {
    cal.hidden = false; apply.hidden = false;
    cal.textContent = "";
    var head = el("div", "cal-nav");
    var prev = el("button", "cal-page", "‹"); prev.type = "button"; prev.setAttribute("aria-label", "Previous month");
    var next = el("button", "cal-page", "›"); next.type = "button"; next.setAttribute("aria-label", "Next month");
    prev.disabled = first <= monthStart(MIN);
    next.disabled = first >= lastFirst();
    prev.onclick = function () { page(-1); };
    next.onclick = function () { page(1); };
    head.appendChild(prev); head.appendChild(next);
    cal.appendChild(head);
    grid = el("div", "cal-months");
    for (var i = 0; i < shown(); i++) grid.appendChild(month(addMonths(first, i)));
    cal.appendChild(grid);
    tint();
    paint();
    load();
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  function month(m) {
    var box = el("div", "cal-month");
    var d = parse(m);
    var title = el("div", "cal-title", MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear());
    title.id = "cal-t-" + m; title.setAttribute("aria-live", "polite");
    box.appendChild(title);
    var rows = el("div", "cal-grid"); rows.setAttribute("role", "grid"); rows.setAttribute("aria-labelledby", title.id);
    WEEKDAYS.forEach(function (w) { rows.appendChild(el("span", "cal-wd", w)); });
    var lead = (d.getUTCDay() + 6) % 7;   // Monday-first
    for (var i = 0; i < lead; i++) rows.appendChild(el("span", "cal-pad"));
    for (var s = m; s.slice(0, 7) === m.slice(0, 7); s = addDays(s, 1)) {
      var b = el("button", "cal-day", String(+s.slice(8)));
      b.type = "button"; b.dataset.day = s;
      b.dataset.label = parse(s).toLocaleDateString("en-GB",
        { weekday: "short", day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
      b.setAttribute("aria-label", b.dataset.label);
      if (!inRange(s)) b.disabled = true;
      if (s === MAX) b.classList.add("today");
      rows.appendChild(b);
    }
    box.appendChild(rows);
    return box;
  }

  /* ---- spend heat: per-day costs fetched for the shown months, tinted relative to the biggest day loaded ---- */
  var costs = {}, asked = {};   // day -> cost; month-start -> true once requested
  function tint() {
    var max = 0, s;
    for (s in costs) if (costs[s] > max) max = costs[s];
    cal.querySelectorAll(".cal-day").forEach(function (b) {
      var c = costs[b.dataset.day], label = b.dataset.label;
      if (c === undefined) { b.removeAttribute("data-heat"); b.title = ""; b.setAttribute("aria-label", label); return; }
      var text = "$" + c.toFixed(2);
      if (c > 0 && max > 0) b.dataset.heat = Math.min(4, Math.max(1, Math.ceil(4 * Math.sqrt(c / max))));
      else b.removeAttribute("data-heat");
      b.title = label + " · " + text;
      b.setAttribute("aria-label", label + ", " + text);
    });
  }
  function load() {
    if (!pop.dataset.costs) return;
    for (var i = 0; i < shown(); i++) {
      (function (m) {
        if (asked[m]) return;
        asked[m] = true;
        var last = addDays(addMonths(m, 1), -1);
        fetch(pop.dataset.costs + "?start=" + clampDay(m < MIN ? MIN : m) + "&end=" + clampDay(last))
          .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
          .then(function (j) { j.days.forEach(function (d) { costs[d.day] = d.cost; }); tint(); })
          .catch(function () { delete asked[m]; });   // untinted until the next page/open retries
      })(addMonths(first, i));
    }
  }

  function paint() {
    var lo = start, hi = end || (hover && hover >= start ? hover : null);
    cal.querySelectorAll(".cal-day").forEach(function (b) {
      var s = b.dataset.day;
      b.classList.toggle("sel-start", s === start);
      b.classList.toggle("sel-end", !!hi && s === hi);
      b.classList.toggle("in-band", !!hi && s > lo && s < hi);
      b.classList.toggle("band-r", s === start && !!hi && hi !== lo);
      b.classList.toggle("band-open", !end && !!hi && s === hi && hi !== lo);   // hover preview end
      b.tabIndex = s === focus ? 0 : -1;
      if (s === start || s === end) b.setAttribute("aria-pressed", "true"); else b.removeAttribute("aria-pressed");
    });
    apply.disabled = !(start && end);
  }

  function page(n) {
    first = clampFirst(addMonths(first, n));
    if (focus < first || focus > addDays(addMonths(first, shown()), -1)) focus = clampDay(first > MIN ? first : MIN);
    build();
  }

  function pick(s) {
    if (!inRange(s)) return;
    if (!start || end || s < start) { start = s; end = null; } else { end = s; }
    hover = null; focus = s;
    paint();
  }

  /* ---- events ---- */
  function dayOf(e) { var b = e.target.closest && e.target.closest(".cal-day"); return b && !b.disabled ? b : null; }
  cal.addEventListener("click", function (e) { var b = dayOf(e); if (b) pick(b.dataset.day); });
  cal.addEventListener("mouseover", function (e) {
    var b = dayOf(e);
    if (b && start && !end) { hover = b.dataset.day; paint(); }
  });
  cal.addEventListener("mouseleave", function () { if (hover) { hover = null; paint(); } });
  cal.addEventListener("focusin", function (e) {
    var b = dayOf(e);
    if (b) { focus = b.dataset.day; if (start && !end) hover = focus; paint(); }
  });

  var STEP = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
  cal.addEventListener("keydown", function (e) {
    var b = dayOf(e);
    if (!b) return;
    var s = b.dataset.day, t = null;
    if (STEP[e.key]) t = addDays(s, STEP[e.key]);
    else if (e.key === "PageUp") t = addMonths(s, -1);
    else if (e.key === "PageDown") t = addMonths(s, 1);
    else if (e.key === "Home") t = addDays(s, -((parse(s).getUTCDay() + 6) % 7));
    else if (e.key === "End") t = addDays(s, 6 - ((parse(s).getUTCDay() + 6) % 7));
    if (t === null) return;   // Enter / Space click the focused button natively
    e.preventDefault();
    t = clampDay(t);
    focus = t; if (start && !end) hover = t;
    if (t < first || t > addDays(addMonths(first, shown()), -1)) {
      first = clampFirst(t < first ? monthStart(t) : addMonths(monthStart(t), -(shown() - 1)));
      build();
    } else paint();
    var nb = cal.querySelector('.cal-day[data-day="' + t + '"]');
    if (nb) nb.focus();
  });

  apply.addEventListener("click", function () {
    if (start && end) {
      location.href = pop.dataset.apply + "?period=custom&start=" + start + "&end=" + end;
    }
  });

  pop.addEventListener("toggle", function () { if (pop.open) reset(); });
  narrow.addEventListener("change", function () { if (pop.open) reset(); });
})();
