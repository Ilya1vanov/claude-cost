/* The "+ Filter" popover: a list of every filter, then one filter's editor with options fetched
   from /filter-options (narrowed by the other applied filters, searched on the server).
   Apply navigates to the page URL with that filter's values replaced; Cancel, Back, x and Esc
   discard unsaved edits. The chips and their remove links are server-rendered. */
(function () {
  var bar = document.getElementById("filterbar");
  if (!bar) return;
  var pop = bar.querySelector(".filter-pop");
  var add = bar.querySelector(".filter-add");
  var dims = JSON.parse(document.getElementById("filter-dims").textContent);
  var optionsUrl = bar.dataset.optionsUrl;
  var st = null;  // {mode: "list" | "filter", dim, fromChip, draft, names, options, q, seq, cal}
  var timer = null;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function icon(path, size) {
    var s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("width", size); s.setAttribute("height", size); s.setAttribute("viewBox", "0 0 16 16");
    s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "1.8");
    s.setAttribute("stroke-linecap", "round"); s.setAttribute("aria-hidden", "true");
    var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", path); s.appendChild(p);
    return s;
  }
  function button(label, cls, onclick) {
    var b = el("button", cls, label); b.type = "button"; b.addEventListener("click", onclick); return b;
  }
  function iconButton(label, path, onclick) {
    var b = el("button", "fp-icon"); b.type = "button"; b.setAttribute("aria-label", label);
    b.appendChild(icon(path, 14)); b.addEventListener("click", onclick); return b;
  }
  function dimOf(key) { return dims.filter(function (d) { return d.key === key; })[0]; }

  function close(refocus) {
    dropCal();
    st = null; pop.hidden = true; pop.textContent = "";
    add.setAttribute("aria-expanded", "false");
    Array.prototype.forEach.call(bar.querySelectorAll(".chip.editing"), function (c) { c.classList.remove("editing"); });
    if (refocus) add.focus();
  }
  function openList() {
    st = { mode: "list" };
    pop.hidden = false; pop.style.left = "0"; add.setAttribute("aria-expanded", "true");
    render();
  }
  function openDim(key, fromChip) {
    var d = dimOf(key);
    st = { mode: "filter", dim: d, fromChip: fromChip, draft: d.values.slice(), names: {}, options: [], q: "", seq: 0 };
    pop.hidden = false; add.setAttribute("aria-expanded", "true");
    var chip = bar.querySelector('.chip[data-dim="' + key + '"]');
    Array.prototype.forEach.call(bar.querySelectorAll(".chip.editing"), function (c) { c.classList.remove("editing"); });
    if (fromChip && chip) { chip.classList.add("editing"); pop.style.left = chip.offsetLeft + "px"; }
    else pop.style.left = "0";
    render();
    if (d.kind === "options") fetchOptions();
  }

  function head(title, back) {
    var h = el("div", "fp-head");
    if (back) h.appendChild(iconButton("Back to all filters", "M10 3L5 8l5 5", openList));
    h.appendChild(el("strong", null, title));
    h.appendChild(iconButton("Close", "M4 4l8 8M12 4l-8 8", function () { close(true); }));
    return h;
  }

  function dropCal() { if (st && st.cal) { st.cal.destroy(); st.cal = null; } }

  function render() {
    dropCal();
    pop.textContent = ""; pop.classList.remove("wide");
    if (st.mode === "list") {
      pop.setAttribute("aria-label", "Add a filter");
      pop.appendChild(head("Add a filter", false));
      var ul = el("ul", "fp-list");
      dims.forEach(function (d) {
        var li = el("li");
        var b = button(null, "fp-row" + (d.values.length ? " on" : ""), function () { openDim(d.key, false); });
        var name = el("span"); name.appendChild(el("span", "dot")); name.appendChild(document.createTextNode(d.label));
        b.appendChild(name); b.appendChild(el("small", null, d.summary));
        li.appendChild(b); ul.appendChild(li);
      });
      pop.appendChild(ul);
      return;
    }
    var d = st.dim;
    pop.setAttribute("aria-label", d.label + " filter");
    pop.appendChild(head(d.label, !st.fromChip));
    var foot = el("div", "fp-foot");
    var note = el("span");
    if (d.kind === "date") {
      dateView(d, note);
    } else {
      var sw = el("div", "fp-search");
      var q = el("input"); q.type = "search"; q.id = "fp-q"; q.value = st.q; q.autocomplete = "off";
      q.placeholder = "Search " + d.label.toLowerCase();
      q.setAttribute("aria-label", "Search " + d.label);
      q.addEventListener("input", function () {
        st.q = q.value; clearTimeout(timer); timer = setTimeout(fetchOptions, 200);
      });
      sw.appendChild(q); pop.appendChild(sw);
      var list = el("ul", "fp-list"); list.id = "fp-options"; pop.appendChild(list);
      fillOptions();
      note.id = "fp-note";
    }
    foot.appendChild(note);
    var btns = el("div");
    btns.appendChild(button("Cancel", "ghost", function () { close(true); }));
    btns.appendChild(button("Apply", "fp-apply", apply));
    foot.appendChild(btns); pop.appendChild(foot);
    updateNote();
    var first = pop.querySelector("input"); if (first) first.focus();
  }

  function updateNote() {
    var n = document.getElementById("fp-note");
    if (n && st.dim.kind === "date") n.textContent = "A preset stays relative when bookmarked";
    else if (n) n.textContent = st.draft.length + " selected";
  }

  /* The Date view: preset radios beside a range calendar for a custom period (both ends included).
     The draft is one value: a preset token, a day, or "A..B". */
  var SHORT = { month: "short", day: "numeric", timeZone: "UTC" };
  function dayText(s) { return new Date(s + "T00:00:00Z").toLocaleDateString("en-US", SHORT); }
  function rangeText(lo, hi) {
    var n = Math.round((Date.parse(hi) - Date.parse(lo)) / 864e5) + 1;
    return dayText(lo) + " \u2013 " + dayText(hi) + " \u00b7 " + n + (n === 1 ? " day" : " days") + ", both ends included";
  }
  function dateView(d, note) {
    var presets = d.presets.map(function (p) { return { value: p[0], label: p[1] }; });
    var tokens = presets.map(function (p) { return p.value; });
    var cur = st.draft[0] || "";
    var custom = cur && tokens.indexOf(cur) < 0 ? cur.split("..") : null;  // [lo] or [lo, hi]
    var wrap = el("div", "fp-date");
    var set = el("fieldset", "fp-presets");
    set.appendChild(el("legend", "sr-only", "Preset"));
    presets.concat([{ value: "", label: "Custom period" }]).forEach(function (p) {
      var lab = el("label"); var r = el("input"); r.type = "radio"; r.name = "date-preset"; r.value = p.value;
      r.checked = p.value ? cur === p.value : !!custom;
      lab.classList.toggle("on", r.checked);
      r.addEventListener("change", function () {
        if (p.value) st.draft = [p.value];
        Array.prototype.forEach.call(set.querySelectorAll("label"), function (l) {
          l.classList.toggle("on", l.querySelector("input").checked);
        });
        if (!p.value) { var g = st.cal.get(); if (g.start && g.end) st.draft = [g.start + ".." + g.end]; }
      });
      lab.appendChild(r); lab.appendChild(document.createTextNode(p.label)); set.appendChild(lab);
    });
    var side = el("div", "fp-calbox");
    var cal = el("div", "period-cal"); cal.hidden = true;
    var summary = el("p", "fp-range");
    side.appendChild(cal); side.appendChild(summary);
    wrap.appendChild(set); wrap.appendChild(side); pop.appendChild(wrap);
    pop.classList.add("wide");
    st.cal = RangeCalendar.mount(cal, {
      min: d.min, max: d.max, costsUrl: d.costsUrl, months: 1,
      onChange: function (s, e) {
        if (!(s && e)) { summary.textContent = s ? dayText(s) + " \u2013 pick the last day" : ""; return; }
        summary.textContent = rangeText(s, e);
        st.draft = [s === e ? s : s + ".." + e];
        var radios = set.querySelectorAll("input");
        Array.prototype.forEach.call(radios, function (r) {
          r.checked = !r.value; r.parentNode.classList.toggle("on", r.checked);
        });
      }
    });
    st.cal.reset(custom && custom[0], custom && (custom[1] || custom[0]));
  }

  function fillOptions() {
    var list = document.getElementById("fp-options");
    if (!list) return;
    list.textContent = "";
    st.options.forEach(function (o) {
      var li = el("li");
      var lab = el("label", "fp-opt" + (st.draft.indexOf(o.value) >= 0 ? " checked" : ""));
      var cb = el("input"); cb.type = "checkbox"; cb.checked = st.draft.indexOf(o.value) >= 0;
      cb.addEventListener("change", function () {
        var i = st.draft.indexOf(o.value);
        if (cb.checked && i < 0) st.draft.push(o.value);
        if (!cb.checked && i >= 0) st.draft.splice(i, 1);
        lab.classList.toggle("checked", cb.checked); updateNote();
      });
      lab.appendChild(cb); lab.appendChild(el("span", null, o.label)); lab.appendChild(el("small", null, String(o.count)));
      li.appendChild(lab); list.appendChild(li);
    });
    if (!st.options.length) list.appendChild(el("li", "fp-empty", st.loaded ? "No values match “" + st.q + "”." : "Loading…"));
  }

  function fetchOptions() {
    var seq = ++st.seq, key = st.dim.key;
    var params = new URLSearchParams(window.location.search);
    params.set("dimension", key); params.set("q", st.q);
    fetch(optionsUrl + "?" + params.toString()).then(function (r) { return r.json(); }).then(function (data) {
      if (!st || st.dim.key !== key || seq !== st.seq) return;  // a newer search or another editor took over
      st.options = data.options; st.loaded = true; fillOptions();
    });
  }

  function apply() {
    var params = new URLSearchParams(window.location.search);
    params.delete(st.dim.key);
    st.draft.forEach(function (v) { params.append(st.dim.key, v); });
    window.location.search = params.toString();
  }

  add.addEventListener("click", function () { if (st && st.mode === "list") close(false); else openList(); });
  Array.prototype.forEach.call(bar.querySelectorAll(".chip-open"), function (b) {
    b.addEventListener("click", function () { openDim(b.dataset.dim, true); });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && st) { e.preventDefault(); close(true); }
  });
  document.addEventListener("click", function (e) {
    if (st && e.composedPath().indexOf(bar) < 0) close(false);
  });
})();
