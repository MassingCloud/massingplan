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

  /**
   * One scheduled activity, as `api.schedules.chart_rows()` sends it.
   *
   * This typedef is the renderer's half of that contract, and it is written
   * down because the half that was not written down went missing: `to_rows()`
   * never carried `predecessors`, the arrow loop read
   * `(row.predecessors || [])`, and the chart drew no dependency arrows at all
   * for weeks. Nothing failed, because `|| []` turns an absent contract into an
   * empty one. `tsc --checkJs` now reads this and the fields are required.
   *
   * @typedef {object} Row
   * @property {string} activity_id   Internal id. 32 hex characters when stored.
   * @property {string} code          The planner's own code -- what to display.
   * @property {string} name          Activity name, for the tooltip.
   * @property {string} start         ISO date, inclusive.
   * @property {string} finish        ISO date, inclusive.
   * @property {number} duration_days Zero for a milestone.
   * @property {number|null} total_float_days  Null when complete: no float, not zero float.
   * @property {boolean} is_critical
   * @property {boolean} is_longest_path
   * @property {boolean} constraint_satisfied
   * @property {string} status
   * @property {string} kind          `task`, `start_milestone`, `finish_milestone`, ...
   * @property {Predecessor[]} predecessors  The arrows are drawn from this.
   */

  /**
   * One relationship, in the same shape `api.schedules` accepts as input --
   * so a response can be fed straight back without flattening every tie to
   * Finish-Start with zero lag.
   *
   * @typedef {object} Predecessor
   * @property {string} id        Predecessor activity id.
   * @property {"FS"|"SS"|"FF"|"SF"} type
   * @property {number} lag_days  Negative is a lead.
   */

  /**
   * Where a bar ended up, so the arrows can find it.
   * @typedef {{x: number, w: number, y: number, mid: number, critical: boolean}} Geometry
   */

  var ROW_H = 26;
  var BAR_H = 14;
  var LEFT = 220;
  var PAD = 16;
  var MIN_DAY_W = 2;
  var MAX_DAY_W = 40;

  /**
   * ISO date to a UTC epoch millisecond, or NaN if it is not one.
   * @param {string|undefined} iso
   * @returns {number}
   */
  function parseDate(iso) {
    var parts = String(iso).split("-");
    if (parts.length !== 3) return NaN;
    return Date.UTC(+parts[0], +parts[1] - 1, +parts[2]);
  }

  /**
   * @param {string} iso
   * @param {number} origin
   * @returns {number}
   */
  function dayIndex(iso, origin) {
    return Math.round((parseDate(iso) - origin) / 86400000);
  }

  /**
   * @param {string} tag
   * @param {Record<string, string|number>} [attrs]
   * @param {string} [text]
   * @returns {SVGElement}
   */
  function el(tag, attrs, text) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        node.setAttribute(key, String(attrs[key]));
      }
    }
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /**
   * @param {number} origin
   * @param {number} totalDays
   * @returns {{offset: number, label: string}[]}
   */
  function monthTicks(origin, totalDays) {
    // Month boundaries rather than an even split: a reader locates "March"
    // instantly and "day 47" never.
    /** @type {{offset: number, label: string}[]} */
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

  /**
   * @param {HTMLElement} host
   * @returns {void}
   */
  function render(host) {
    /** @type {Row[]} */
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

    // A missing or unparseable bound makes every coordinate NaN, and an SVG
    // full of NaN renders as a blank box with no error anywhere. Say so.
    var origin = parseDate(host.dataset.start);
    var last = parseDate(host.dataset.finish);
    if (isNaN(origin) || isNaN(last)) {
      host.textContent = "The schedule has no start or finish date to draw against.";
      return;
    }
    var totalDays = Math.max(1, Math.round((last - origin) / 86400000) + 1);
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
    // `create(null)`, not `{}`: activity ids come from uploaded files, and a
    // row coded "constructor" or "__proto__" would otherwise resolve against
    // Object's prototype and route an arrow to a garbage coordinate.
    /** @type {Record<string, Geometry>} */
    var geometry = Object.create(null);
    var bars = el("g", { class: "bars" });

    rows.forEach(function (/** @type {Row} */ row, /** @type {number} */ i) {
      var y = 40 + i * ROW_H;
      var s = dayIndex(row.start, origin);
      var f = dayIndex(row.finish, origin);
      var x = LEFT + s * dayW;
      var w = Math.max(dayW * 0.6, (f - s + 1) * dayW);
      var milestone = row.duration_days === 0;
      // A *finish* milestone belongs at the end of its day, not the start.
      // Its predecessor's bar occupies that whole day, so drawing the diamond
      // at the day's start put it one day-width left of the bar it marks and
      // the arrow into it ran backwards. A start milestone stays at the front
      // of its day, which is where the work it releases begins.
      var atDayEnd = milestone && row.kind === "finish_milestone";

      // The planner's own code, not the internal id -- on a stored project
      // that id is 32 hex characters and means nothing to the reader.
      var name = row.code || row.activity_id;
      var label = el("text", { x: PAD, y: y + BAR_H - 2, class: "row-label" }, name);
      label.appendChild(el("title", {}, row.name ? name + " -- " + row.name : name));
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
        var cx = atDayEnd ? x + dayW : x;
        var cy = y + BAR_H / 2;
        shape = el("path", {
          d: "M" + cx + "," + (cy - 7) + " L" + (cx + 7) + "," + cy +
             " L" + cx + "," + (cy + 7) + " L" + (cx - 7) + "," + cy + " Z",
          class: cls + " bar-milestone"
        });
      } else {
        shape = el("rect", { x: x, y: y, width: w, height: BAR_H, rx: 2, class: cls });
      }
      var tip = name + "  " + row.start + " to " + row.finish +
        "  (" + row.duration_days + "d)" +
        (row.total_float_days === null
          ? "  float: n/a, complete"
          : "  float: " + row.total_float_days + "d");
      shape.appendChild(el("title", {}, tip));
      bars.appendChild(shape);

      // A milestone is a *point*, so its geometry has no width. Giving it the
      // bar width `w` put the outgoing anchor a full day to the right of the
      // diamond, and an arrow to a successor starting that same day then ran
      // backwards -- 2px at this zoom, 40px zoomed in.
      geometry[row.activity_id] = {
        x: atDayEnd ? x + dayW : x,
        w: milestone ? 0 : w,
        y: y,
        mid: y + BAR_H / 2,
        critical: row.is_longest_path
      };
    });

    // -- dependency arrows -------------------------------------------------
    // Routed as an elbow rather than a straight line: on a real schedule a
    // straight line crosses six unrelated bars and tells the reader nothing.
    //
    // **Each end is anchored by relationship type.** A Start-Start tie
    // constrains the two *starts*, so an arrow drawn from the predecessor's
    // finish is not a picture of that constraint -- and because an SS successor
    // usually starts before its predecessor ends, such an arrow runs
    // right-to-left and reads as though time flows backwards. Every arrow did
    // that for as long as the payload carried bare ids and no type.
    var links = el("g", { class: "links" });
    rows.forEach(function (/** @type {Row} */ row) {
      // Not `row.predecessors || []`. That is what hid the missing contract:
      // an absent key became an empty list and the chart drew nothing, with no
      // error anywhere. If the server stops sending them, this throws.
      row.predecessors.forEach(function (/** @type {Predecessor} */ pred) {
        var from = geometry[pred.id];
        var to = geometry[row.activity_id];
        if (!from || !to) return;

        // FS and FF leave the predecessor's finish; SS and SF leave its start.
        var fromFinish = pred.type === "FS" || pred.type === "FF";
        // FS and SS arrive at the successor's start; FF and SF at its finish.
        var toStart = pred.type === "FS" || pred.type === "SS";

        var x1 = fromFinish ? from.x + from.w : from.x;
        var y1 = from.mid;
        var x2 = toStart ? to.x : to.x + to.w;
        var y2 = to.mid;
        var gap = Math.max(6, dayW);

        // Two routes. Forward: out of the source, across, into the target.
        // Backward (x2 behind x1, which a lead or an SF tie can produce
        // legitimately): step out past both ends and come back, so the line
        // stays readable instead of doubling back through the bars it links.
        // A tie between two points at the same instant -- a milestone and the
        // activity it gates -- lands within a day-width of itself. That is a
        // short hook, not a backwards arrow, and routing it the long way round
        // makes a correct schedule look wrong. The jog is for links that really
        // do run backwards: a lead, or a Start-Finish tie.
        var path;
        if (x2 >= x1 - dayW) {
          path = "M" + x1 + "," + y1 +
            " H" + (x1 + gap / 2) +
            " V" + y2 +
            " H" + x2;
        } else {
          var lane = Math.max(y1, y2) + ROW_H / 2 - 3;
          path = "M" + x1 + "," + y1 +
            " H" + (x1 + gap / 2) +
            " V" + lane +
            " H" + (x2 - gap / 2) +
            " V" + y2 +
            " H" + x2;
        }

        var driving = from.critical && to.critical;
        var arrow = el("path", {
          d: path,
          class: driving ? "link link-critical" : "link",
          "marker-end": "url(#" + (driving ? "arrow-critical" : "arrow") + ")"
        });
        // The type is worth saying out loud: an SS tie drawn correctly still
        // looks surprising next to an FS one, and the tooltip is the cheapest
        // place to explain why.
        arrow.appendChild(el("title", {},
          pred.id + " -> " + (row.code || row.activity_id) + "  " + pred.type +
          (pred.lag_days ? (pred.lag_days > 0 ? "+" : "") + pred.lag_days + "d" : "")));
        links.appendChild(arrow);
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
      render(/** @type {HTMLElement} */ (hosts[i]));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  /** @type {number|undefined} */
  var resizeTimer;
  window.addEventListener("resize", function () {
    if (resizeTimer !== undefined) window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(boot, 150);
  });
})();
