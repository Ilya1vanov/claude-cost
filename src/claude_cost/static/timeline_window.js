// Timeline windowing: only the rows near the scroll position are in the DOM, while the scrollbar
// spans the whole session. Rows are drawn by window.sessionTimeline.renderRow (session.js), so a
// new row kind needs no change here. Loaded as a module script, so it runs after session.js.
import { Virtualizer, elementScroll, observeElementRect, observeElementOffset } from "./vendor/virtual-core.esm.js";

const api = window.sessionTimeline;
if (api) {
  const body = api.body;
  const ESTIMATED_ROW_HEIGHT = 72;  // until a row has been measured
  const OVERSCAN = 8;
  const firstVisibleListeners = [];
  const visibleRangeListeners = [];
  const rowsListeners = [];
  let rows = [];
  let space = null;
  let virtualizer = null;
  let firstVisible = -1;
  let lastVisible = -1;
  const nodes = new Map();  // row index -> its wrapper element, for the rows currently drawn

  // The sticky badge in the Timeline header: the setting in effect for the first visible row
  // (a Subagent's own while that row is inside its expanded block).
  const settingBox = document.getElementById("timeline-setting");
  let shownSetting = null;
  function updateSettingBadge() {
    if (!settingBox) { return; }
    const s = firstVisible >= 0 ? api.settingAt(firstVisible) : null;
    const key = s ? [s.model, s.effort].join("|") : "";
    if (key === shownSetting) { return; }
    shownSetting = key;
    settingBox.textContent = "";
    if (s) { api.settingBadges(s, settingBox); }
  }

  function render() {
    const items = virtualizer.getVirtualItems();
    const wanted = new Set(items.map(function (i) { return i.index; }));
    nodes.forEach(function (node, index) {
      if (!wanted.has(index)) { node.remove(); nodes.delete(index); }
    });
    items.forEach(function (item) {
      let node = nodes.get(item.index);
      if (!node) {
        const content = api.renderRow(rows[item.index]);
        node = api.el("div", "tl-item");
        node.dataset.index = String(item.index);
        if (content) { node.appendChild(content); }
        space.appendChild(node);
        nodes.set(item.index, node);
      }
      node.style.transform = "translateY(" + item.start + "px)";
      virtualizer.measureElement(node);  // observes the node: later size changes are re-measured too
    });
    space.style.height = virtualizer.getTotalSize() + "px";
    const start = virtualizer.range ? virtualizer.range.startIndex : (rows.length ? 0 : -1);
    if (start !== firstVisible) {
      firstVisible = start;
      firstVisibleListeners.slice().forEach(function (fn) { fn(start); });
    }
    const end = virtualizer.range ? virtualizer.range.endIndex : start;
    if (end !== lastVisible) {
      lastVisible = end;
      visibleRangeListeners.slice().forEach(function (fn) { fn({ start: start, end: end }); });
    }
    updateSettingBadge();
  }

  function notifyRows() { rowsListeners.slice().forEach(function (fn) { fn(rows); }); }

  function mount(newRows) {
    rows = newRows;
    body.textContent = "";
    space = api.el("div", "tl-space");
    body.appendChild(space);
    nodes.clear();
    firstVisible = -1;
    lastVisible = -1;
    if (virtualizer) { virtualizer._didMount()(); }  // clean up a previous mount's observers
    virtualizer = new Virtualizer({
      count: rows.length,
      getScrollElement: function () { return body; },
      estimateSize: function () { return ESTIMATED_ROW_HEIGHT; },
      overscan: OVERSCAN,
      scrollToFn: elementScroll,
      observeElementRect: observeElementRect,
      observeElementOffset: observeElementOffset,
      onChange: render,
    });
    virtualizer._didMount();
    virtualizer._willUpdate();
    render();
    notifyRows();
  }

  // For rows that change height without the browser telling us (the ResizeObserver already covers
  // expand and collapse): re-measure row `index` if it is drawn.
  api.remeasure = function (index) {
    const node = nodes.get(index);
    if (node) { virtualizer.measureElement(node); }
  };
  api.scrollToIndex = function (index, options) { if (virtualizer) { virtualizer.scrollToIndex(index, options); } };
  api.getFirstVisibleIndex = function () { return firstVisible; };
  // fn(index) runs whenever the first row in view changes; returns a function that unsubscribes.
  api.onFirstVisibleChange = function (fn) {
    firstVisibleListeners.push(fn);
    return function () { const i = firstVisibleListeners.indexOf(fn); if (i >= 0) { firstVisibleListeners.splice(i, 1); } };
  };
  // The rows currently listed (Subagent blocks expanded or not), and the last row in view.
  api.getRows = function () { return rows; };
  api.getVisibleRange = function () { return { start: firstVisible, end: lastVisible }; };
  // fn({start, end}) runs whenever the first or last row in view changes.
  api.onVisibleRangeChange = function (fn) {
    visibleRangeListeners.push(fn);
    return function () { const i = visibleRangeListeners.indexOf(fn); if (i >= 0) { visibleRangeListeners.splice(i, 1); } };
  };
  // fn(rows) runs after the row list is mounted or swapped (a Subagent block expanded or collapsed).
  api.onRowsChange = function (fn) {
    rowsListeners.push(fn);
    return function () { const i = rowsListeners.indexOf(fn); if (i >= 0) { rowsListeners.splice(i, 1); } };
  };
  // Open (or close, with on=false) the text of row `index` while Collapse text is on; the
  // ResizeObserver re-measures the drawn row. For the search (#92) to reveal a match.
  api.expandText = function (index, on) {
    if (!rows[index]) { return; }
    const open = on !== false;
    api.setRowExpanded(rows[index], open);
    const node = nodes.get(index);
    if (node && node.firstChild) { node.firstChild.classList.toggle("expanded", open); }
  };
  api.mountRows = mount;
  // Swap in a changed row list (a Subagent block expanded or collapsed) keeping the scroll position.
  api.updateRows = function (newRows) {
    if (!virtualizer) { mount(newRows); return; }
    rows = newRows;
    nodes.forEach(function (node) { node.remove(); });
    nodes.clear();
    virtualizer.setOptions(Object.assign({}, virtualizer.options, { count: rows.length }));
    virtualizer.measure();  // item sizes were cached per index, which has shifted
    render();
    notifyRows();
  };

  api.loadRows(document.getElementById("timeline-box").dataset.src).then(api.setTree || mount).catch(function (err) {
    body.textContent = "";
    body.appendChild(api.el("p", "empty", "Could not load the Timeline: " + err.message));
  });
}
