// Multi-chip Session picker: renders chips and suggestions from the search endpoint.
// All ranking happens on the server; this script only displays what it returns.
(function () {
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  document.querySelectorAll(".picker").forEach(function (root) {
    var collect = root.dataset.mode === "collect";
    var input = root.querySelector(".picker-input");
    var list = root.querySelector(".picker-list");
    var chips = root.querySelector(".chips");
    var idField = root.querySelector("input[name=session]");
    var picked = [];
    var latest = 0;

    function addChip(c) {
      picked.push(c.id);
      var chip = el("span", "chip", c.label);
      var hidden = el("input");
      hidden.type = "hidden";
      hidden.name = "session";
      hidden.value = c.id;
      var x = el("button", "chip-x", "×");
      x.type = "button";
      x.setAttribute("aria-label", "Remove " + c.label);
      x.addEventListener("click", function () {
        picked.splice(picked.indexOf(c.id), 1);
        chip.remove();
      });
      chip.append(hidden, x);
      chips.append(chip);
    }

    function choose(c) {
      list.hidden = true;
      input.value = "";
      if (collect) {
        addChip(c);
        input.focus();
      } else {
        idField.value = c.id;
        idField.form.submit();
      }
    }

    function show(results, empty) {
      list.replaceChildren();
      if (!results.length) { list.hidden = true; return; }
      if (empty) list.append(el("li", "picker-head", "Suggested: Forks"));
      results.forEach(function (c) {
        var li = el("li", "picker-item");
        li.append(el("span", "picker-label", c.label), el("span", "id", c.short_id),
                  el("span", "num", "$" + c.cost.toFixed(2)));
        if (c.kind) li.append(el("span", "badge", c.kind));
        li.addEventListener("click", function () { choose(c); });
        list.append(li);
      });
      list.hidden = false;
    }

    function search() {
      var n = ++latest;
      var params = new URLSearchParams({ q: input.value, name: root.dataset.name || "" });
      picked.forEach(function (id) { params.append("exclude", id); });
      fetch(root.dataset.searchUrl + "?" + params)
        .then(function (r) { return r.json(); })
        .then(function (d) { if (n === latest) show(d.results, input.value.trim() === ""); });
    }

    input.addEventListener("focus", search);
    input.addEventListener("input", search);
    input.addEventListener("blur", function () { setTimeout(function () { list.hidden = true; }, 150); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") list.hidden = true;
      if (e.key === "Enter") {
        e.preventDefault();
        var first = list.querySelector(".picker-item");
        if (first) first.click();
      }
    });
  });
})();
