/**
 * A Gantt chart, self-hosted, no framework, no dependency.
 *
 * This file is the one deliberate departure from the sibling repo's zero-JS
 * rule (AGENTS.md, "One deliberate deviation"). A schedule tool nobody can drag
 * a bar in is a report, not a tool. It is written as plain DOM + inline SVG so
 * the strict `default-src 'self'` CSP stays exactly as strict as it is -- no
 * chart library, no CDN, no external font.
 *
 * What it draws that the source systems did not:
 *   - dependency arrows, routed so they are followable rather than straight
 *     lines through other bars;
 *   - the driving path highlighted as a *chain*, not as a bag of zero-float
 *     activities;
 *   - float shown as a tail on the bar, so slack is visible rather than being a
 *     number in a column nobody reads;
 *   - completed work drawn distinctly, because its dates are history and the
 *     reader should not be looking for float on it.
 */
(function () {
  "use strict";

  var ROW_H = 26;
  var BAR_H = 14;
  var LEFT = 220;
  var PAD = 16;
  var MIN_DAY_W = 2;
  var MAX_DAY_W = 40;

  function parseDate(iso) {
    var parts = String(iso).split("-");
    return Date.UTC(+parts[0], +parts[1] - 1, +parts[2]);
  }

  function dayIndex(iso, origin) {
    return Math.round((parseDate(iso) - origin) / 86400000);
  }

  function el(tag, attrs, text) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        node.setAttribute(key, attrs[key]);
      }
    }
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function monthTicks(origin, totalDays) {
    // Month boundaries rather than an even split: a reader locates "March"
    // instantly and "day 47" never.
    var ticks = [];
    var cursor = new Date(origin);
    cursor.setUTCDate(1);
    while (true) {
      var offset = Math.round((cursor.getTime() - origin) / 86400000);
      if (offset > totalDays) break;
      if (offset >= 0) {
        ticks.push({
          offset: offset,
          label: cursor.toLocaleString("en", { month: "short", year: "2-digit", timeZone: "UTC" })
        });
      }
      cursor.setUTCMonth(cursor.getUTCMonth() + 1);
    }
    return ticks;
  }

  function render(host) {
    var rows;
    try {
      rows = JSON.parse(host.dataset.activities || "[]");
    } catch (err) {
      host.textContent = "The schedule data could not be read.";
      return;
    }
    if (!rows.length) {
      host.textContent = "No activities to draw.";
      return;
    }

    var origin = parseDate(host.dataset.start);
    var totalDays = Math.max(1, dayIndex(host.dataset.finish, origin) + 1);
    var available = Math.max(320, host.clientWidth - LEFT - PAD * 2);
    var dayW = Math.min(MAX_DAY_W, Math.max(MIN_DAY_W, available / totalDays));

    var width = LEFT + totalDays * dayW + PAD * 2;
    var height = rows.length * ROW_H + 56;
    var svg = el("svg", {
      viewBox: "0 0 " + width + " " + height,
      width: "100%",
      height: height,
      role: "img",
      "aria-label": "Gantt chart of " + rows.length + " activities"
    });

    var defs = el("defs");
    ["arrow", "arrow-critical"].forEach(function (id) {
      var marker = el("marker", {
        id: id, viewBox: "0 0 8 8", refX: "7", refY: "4",
        markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse"
      });
      marker.appendChild(el("path", {
        d: "M0,0 L8,4 L0,8 z",
        class: id === "arrow" ? "arrowhead" : "arrowhead arrowhead-critical"
      }));
      defs.appendChild(marker);
    });
    svg.appendChild(defs);

    // -- time axis ---------------------------------------------------------
    var axis = el("g", { class: "axis" });
    monthTicks(origin, totalDays).forEach(function (tick) {
      var x = LEFT + tick.offset * dayW;
      axis.appendChild(el("line", { x1: x, y1: 28, x2: x, y2: height - 8, class: "gridline" }));
      axis.appendChild(el("text", { x: x + 3, y: 20, class: "axis-label" }, tick.label));
    });
    svg.appendChild(axis);

    // -- bars --------------------------------------------------------------
    var geometry = {};
    var bars = el("g", { class: "bars" });

    rows.forEach(function (row, i) {
      var y = 40 + i * ROW_H;
      var s = dayIndex(row.start, origin);
      var f = dayIndex(row.finish, origin);
      var x = LEFT + s * dayW;
      var w = Math.max(dayW * 0.6, (f - s + 1) * dayW);
      var milestone = row.duration_days === 0;

      var label = el("text", { x: PAD, y: y + BAR_H - 2, class: "row-label" },
        row.activity_id);
      label.appendChild(el("title", {}, row.activity_id));
      bars.appendChild(label);
      bars.appendChild(el("line", {
        x1: LEFT, y1: y + BAR_H + 6, x2: width - PAD, y2: y + BAR_H + 6, class: "rowline"
      }));

      // Float drawn as a tail, so slack is visible rather than a number in a
      // column. Only forward float, and only where it exists.
      if (row.total_float_days !== null && row.total_float_days > 0 && !milestone) {
        bars.appendChild(el("rect", {
          x: x + w, y: y + BAR_H / 2 - 2,
          width: Math.max(1, row.total_float_days * dayW), height: 4,
          class: "float-tail"
        }));
      }

      var cls = "bar";
      if (row.status === "complete") cls += " bar-complete";
      else if (row.is_longest_path) cls += " bar-driving";
      else if (row.is_critical) cls += " bar-critical";
      if (!row.constraint_satisfied) cls += " bar-violation";

      var shape;
      if (milestone) {
        var cx = x, cy = y + BAR_H / 2;
        shape = el("path", {
          d: "M" + cx + "," + (cy - 7) + " L" + (cx + 7) + "," + cy +
             " L" + cx + "," + (cy + 7) + " L" + (cx - 7) + "," + cy + " Z",
          class: cls + " bar-milestone"
        });
      } else {
        shape = el("rect", { x: x, y: y, width: w, height: BAR_H, rx: 2, class: cls });
      }
      var tip = row.activity_id + "  " + row.start + " to " + row.finish +
        "  (" + row.duration_days + "d)" +
        (row.total_float_days === null
          ? "  float: n/a, complete"
          : "  float: " + row.total_float_days + "d");
      shape.appendChild(el("title", {}, tip));
      bars.appendChild(shape);

      geometry[row.activity_id] = { x: x, w: w, y: y, mid: y + BAR_H / 2, critical: row.is_longest_path };
    });

    // -- dependency arrows -------------------------------------------------
    // Routed as an elbow rather than a straight line: on a real schedule a
    // straight line crosses six unrelated bars and tells the reader nothing.
    var links = el("g", { class: "links" });
    rows.forEach(function (row) {
      (row.predecessors || []).forEach(function (predId) {
        var from = geometry[predId];
        var to = geometry[row.activity_id];
        if (!from || !to) return;
        var x1 = from.x + from.w;
        var y1 = from.mid;
        var x2 = to.x;
        var y2 = to.mid;
        var gap = Math.max(6, dayW);
        var path = "M" + x1 + "," + y1 +
          " H" + (x1 + gap / 2) +
          " V" + y2 +
          " H" + x2;
        var driving = from.critical && to.critical;
        links.appendChild(el("path", {
          d: path,
          class: driving ? "link link-critical" : "link",
          "marker-end": "url(#" + (driving ? "arrow-critical" : "arrow") + ")"
        }));
      });
    });

    svg.appendChild(links);
    svg.appendChild(bars);
    host.textContent = "";
    host.appendChild(svg);
  }

  function boot() {
    var hosts = document.querySelectorAll(".gantt-host");
    for (var i = 0; i < hosts.length; i++) {
      render(hosts[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(boot, 150);
  });
})();
