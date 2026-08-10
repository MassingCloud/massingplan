/**
 * Line of balance: where every crew is, and where they close on each other.
 *
 * A Gantt answers "when". This answers "where", and it is the only drawing that
 * shows the thing that actually goes wrong on a repetitive job -- two trades
 * converging on the same floor. On a bar chart those two bars simply sit next
 * to each other and look fine.
 *
 * Time runs left to right; **location runs up the page** in flow order, so a
 * trade climbing a building climbs the chart. Each trade is one polyline: a
 * step per location, sloping right as it goes. The slope *is* the production
 * rate -- a steep line is a slow trade -- and two lines that converge are two
 * crews running out of room between them.
 *
 * No framework, no chart library, no CDN. Same rule as the Gantt: the strict
 * `default-src 'self'` CSP stays exactly as strict as it is.
 */
(function () {
  "use strict";

  /**
   * One task in one location, as `api.schedules.schedule_linear()` sends it.
   *
   * @typedef {object} Segment
   * @property {string} activity_id
   * @property {string} task_id
   * @property {string} location_id
   * @property {string} start          ISO date, inclusive.
   * @property {string} finish         ISO date, inclusive.
   * @property {number} duration_days
   */

  /**
   * Where two consecutive trades come closest.
   *
   * @typedef {object} Interference
   * @property {string} predecessor_id
   * @property {string} successor_id
   * @property {string} location_id   The binding location.
   * @property {number} gap_days
   * @property {boolean} converging
   */

  var LEFT = 110;
  var PAD = 18;
  var TOP = 34;
  var ROW_H = 30;
  var MIN_DAY_W = 3;
  var MAX_DAY_W = 26;

  /**
   * @param {string} iso
   * @returns {number}
   */
  function parseDate(iso) {
    var parts = String(iso).split("-");
    if (parts.length !== 3) return NaN;
    return Date.UTC(+parts[0], +parts[1] - 1, +parts[2]);
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
   * @param {HTMLElement} host
   * @returns {void}
   */
  function render(host) {
    /** @type {Segment[]} */
    var segments;
    /** @type {Interference[]} */
    var interferences;
    try {
      segments = JSON.parse(host.dataset.segments || "[]");
      interferences = JSON.parse(host.dataset.interferences || "[]");
    } catch (err) {
      host.textContent = "The linear schedule could not be read.";
      return;
    }
    if (!segments.length) {
      host.textContent = "No linear schedule to draw.";
      return;
    }

    // Location order comes from the payload, which is already in flow order.
    // Deriving it by sorting the ids would put "L10" before "L2".
    /** @type {string[]} */
    var locations = [];
    /** @type {string[]} */
    var tasks = [];
    segments.forEach(function (/** @type {Segment} */ s) {
      if (locations.indexOf(s.location_id) === -1) locations.push(s.location_id);
      if (tasks.indexOf(s.task_id) === -1) tasks.push(s.task_id);
    });

    var origin = Infinity;
    var last = -Infinity;
    segments.forEach(function (/** @type {Segment} */ s) {
      origin = Math.min(origin, parseDate(s.start));
      last = Math.max(last, parseDate(s.finish));
    });
    if (!isFinite(origin) || !isFinite(last)) {
      host.textContent = "The linear schedule has no dates to draw against.";
      return;
    }

    var totalDays = Math.max(1, Math.round((last - origin) / 86400000) + 1);
    var available = Math.max(320, host.clientWidth - LEFT - PAD * 2);
    var dayW = Math.min(MAX_DAY_W, Math.max(MIN_DAY_W, available / totalDays));
    var width = LEFT + totalDays * dayW + PAD * 2;
    var height = TOP + locations.length * ROW_H + 46;

    var svg = el("svg", {
      viewBox: "0 0 " + width + " " + height,
      width: "100%",
      height: height,
      role: "img",
      "aria-label":
        "Line of balance: " + tasks.length + " trades across " + locations.length + " locations"
    });

    /** @param {string} iso */
    function x(iso) {
      return LEFT + Math.round((parseDate(iso) - origin) / 86400000) * dayW;
    }
    /**
     * Location runs *up* the page: the first location is at the bottom, so a
     * trade working its way up a building works its way up the chart. Drawn
     * top-down with the order reversed would read as descending, which is the
     * opposite of what the crew is doing.
     * @param {string} locationId
     */
    function y(locationId) {
      var index = locations.indexOf(locationId);
      return TOP + (locations.length - 1 - index) * ROW_H;
    }

    // -- location bands ----------------------------------------------------
    var bands = el("g", { class: "lob-bands" });
    locations.forEach(function (/** @type {string} */ locationId, /** @type {number} */ i) {
      var top = y(locationId) - ROW_H / 2;
      if (i % 2 === 0) {
        bands.appendChild(
          el("rect", {
            x: LEFT, y: top, width: width - LEFT - PAD, height: ROW_H, class: "lob-band"
          })
        );
      }
      bands.appendChild(
        el("text", { x: PAD, y: y(locationId) + 4, class: "lob-location" }, locationId)
      );
    });
    svg.appendChild(bands);

    // -- one polyline per trade -------------------------------------------
    // The line, not the bars, is the point: its slope is the production rate,
    // and two lines closing on each other is the conflict a Gantt hides.
    var lines = el("g", { class: "lob-lines" });
    tasks.forEach(function (/** @type {string} */ taskId, /** @type {number} */ index) {
      var mine = segments.filter(function (/** @type {Segment} */ s) {
        return s.task_id === taskId;
      });
      /** @type {string[]} */
      var points = [];
      mine.forEach(function (/** @type {Segment} */ s) {
        // Two points per location: the crew enters and leaves. The horizontal
        // run between them is the time spent there, so the flat sections are
        // the work and the rises are the moves.
        points.push(x(s.start) + "," + y(s.location_id));
        points.push(x(s.finish) + dayW + "," + y(s.location_id));
      });

      var cls = "lob-line lob-line-" + (index % 6);
      lines.appendChild(el("polyline", { points: points.join(" "), class: cls }));
      mine.forEach(function (/** @type {Segment} */ s) {
        var marker = el("circle", {
          cx: x(s.start), cy: y(s.location_id), r: 2.5, class: cls + " lob-node"
        });
        marker.appendChild(
          el("title", {}, taskId + "  " + s.location_id + "  " + s.start + " to " + s.finish)
        );
        lines.appendChild(marker);
      });

      var first = mine[0];
      if (first) {
        lines.appendChild(
          el(
            "text",
            { x: x(first.start) + 4, y: y(first.location_id) - 6, class: "lob-label " + cls },
            taskId
          )
        );
      }
    });
    svg.appendChild(lines);

    // -- the binding location ----------------------------------------------
    // Marked because it is the actionable output: the one place where the
    // buffer between two trades is fully consumed, and therefore where a slip
    // is felt first. "Tight on level 7" is something a planner can act on.
    var marks = el("g", { class: "lob-marks" });
    interferences.forEach(function (/** @type {Interference} */ hit) {
      var successor = segments.filter(function (/** @type {Segment} */ s) {
        return s.task_id === hit.successor_id && s.location_id === hit.location_id;
      })[0];
      if (!successor) return;
      var mark = el("rect", {
        x: x(successor.start) - 4,
        y: y(hit.location_id) - 9,
        width: 8,
        height: 18,
        class: hit.gap_days < 0 ? "lob-bind lob-bind-overlap" : "lob-bind"
      });
      mark.appendChild(
        el(
          "title",
          {},
          hit.predecessor_id + " -> " + hit.successor_id + " at " + hit.location_id +
            ": " + hit.gap_days + " days of clearance" +
            (hit.converging ? ", and closing" : "")
        )
      );
      marks.appendChild(mark);
    });
    svg.appendChild(marks);

    host.textContent = "";
    host.appendChild(svg);
  }

  function boot() {
    var hosts = document.querySelectorAll(".lob-host");
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
