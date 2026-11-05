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
  let rows = [];
  let space = null;
  let virtualizer = null;
  let firstVisible = -1;
  const nodes = new Map();  // row index -> its wrapper element, for the rows currently drawn

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
  }

  function mount(newRows) {
    rows = newRows;
    body.textContent = "";
    space = api.el("div", "tl-space");
    body.appendChild(space);
    nodes.clear();
    firstVisible = -1;
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

  api.loadRows(document.getElementById("timeline-box").dataset.src).then(mount).catch(function (err) {
    body.textContent = "";
    body.appendChild(api.el("p", "empty", "Could not load the Timeline: " + err.message));
  });
}
