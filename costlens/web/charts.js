/* CostLens charts: small dependency-free SVG renderer. Values arrive as integer cents. */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function s(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs || {}) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function palette() {
    const cs = getComputedStyle(document.documentElement);
    const p = [];
    for (let i = 1; i <= 8; i++) p.push(cs.getPropertyValue("--c" + i).trim() || "#888");
    return { p, other: cs.getPropertyValue("--c-other").trim() || "#aaa" };
  }
  function colorFor(i, key) {
    const pal = palette();
    return key === "Other" ? pal.other : pal.p[i % pal.p.length];
  }
  function money(cents, compact) {
    const v = cents / 100;
    const sign = v < 0 ? "-" : "";
    const a = Math.abs(v);
    if (compact) {
      if (a >= 1e6) return sign + "$" + (a / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
      if (a >= 1e3) return sign + "$" + (a / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k";
      return sign + "$" + a.toFixed(a >= 100 ? 0 : a >= 10 ? 1 : 2);
    }
    return sign + "$" + a.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function bucketLabel(b, long) {
    if (b === "total") return "Total";
    const parts = b.split("-");
    if (parts.length === 2) return MONTHS[+parts[1] - 1] + " " + parts[0];
    const d = +parts[2], m = MONTHS[+parts[1] - 1];
    return long ? d + " " + m + " " + parts[0] : d + " " + m;
  }
  function niceTicks(min, max, count) {
    if (min === max) { max = min + 100; }
    const span = max - min;
    const raw = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.round(v));
    return { lo, hi, ticks };
  }
  function truncate(t, n) { return t.length > n ? t.slice(0, n - 1) + "…" : t; }

  const tip = () => document.getElementById("chartTip");
  function showTip(evt, title, rows, total) {
    const t = tip();
    if (!t) return;
    t.textContent = "";
    const h = document.createElement("div");
    h.className = "tt-h";
    h.textContent = title;
    t.appendChild(h);
    rows.forEach(r => {
      const row = document.createElement("div");
      row.className = "tt-r";
      const l = document.createElement("span");
      const sw = document.createElement("i");
      sw.style.background = r.color;
      l.appendChild(sw);
      l.appendChild(document.createTextNode(truncate(r.name, 30)));
      const v = document.createElement("span");
      v.textContent = money(r.value);
      row.appendChild(l); row.appendChild(v);
      t.appendChild(row);
    });
    if (total !== undefined) {
      const row = document.createElement("div");
      row.className = "tt-r";
      row.style.borderTop = "1px solid rgba(127,127,127,.4)";
      row.style.marginTop = "3px";
      row.style.paddingTop = "3px";
      const l = document.createElement("span"); l.textContent = "Total";
      const v = document.createElement("span"); v.textContent = money(total);
      row.appendChild(l); row.appendChild(v); t.appendChild(row);
    }
    t.hidden = false;
    const x = Math.min(evt.clientX + 14, window.innerWidth - t.offsetWidth - 8);
    const y = Math.min(evt.clientY + 14, window.innerHeight - t.offsetHeight - 8);
    t.style.left = x + "px";
    t.style.top = y + "px";
  }
  function hideTip() { const t = tip(); if (t) t.hidden = true; }

  function axes(svg, W, H, m, scaleY, ticks) {
    const g = s("g", { class: "grid" }, svg);
    ticks.forEach(v => {
      const y = scaleY(v);
      s("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, class: v === 0 ? "zero" : "" }, g);
      const t = s("text", { x: m.l - 8, y: y + 4, "text-anchor": "end" }, svg);
      t.textContent = money(v, true);
    });
  }

  function vertical(container, result, type) {
    const W = Math.max(320, container.clientWidth || 640), H = 260;
    const m = { l: 56, r: 12, t: 12, b: 26 };
    const buckets = result.buckets;
    const series = type === "bar" && result.series.length > 1
      ? [{ key: "Total", name: "Total", values_cents: result.bucket_totals_cents }]
      : result.series;
    let lo = 0, hi = 0;
    buckets.forEach((_, i) => {
      let pos = 0, neg = 0;
      series.forEach(se => {
        const v = se.values_cents[i];
        if (type === "line") { lo = Math.min(lo, v); hi = Math.max(hi, v); }
        else if (v >= 0) pos += v; else neg += v;
      });
      if (type !== "line") { hi = Math.max(hi, pos); lo = Math.min(lo, neg); }
    });
    const nt = niceTicks(lo, hi, 4);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": result.title || "Cost chart" });
    const sy = v => m.t + (H - m.t - m.b) * (1 - (v - nt.lo) / (nt.hi - nt.lo || 1));
    axes(svg, W, H, m, sy, nt.ticks);
    const n = buckets.length;
    const bw = (W - m.l - m.r) / n;
    const step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor((W - m.l) / 64))));
    buckets.forEach((b, i) => {
      if (i % step !== 0) return;
      const t = s("text", { x: m.l + bw * i + bw / 2, y: H - 8, "text-anchor": "middle" }, svg);
      t.textContent = bucketLabel(b);
    });
    const colors = series.map((se, i) => colorFor(i, se.key));
    if (type === "line") {
      series.forEach((se, si) => {
        const pts = se.values_cents.map((v, i) => `${m.l + bw * i + bw / 2},${sy(v)}`).join(" ");
        s("polyline", { points: pts, fill: "none", stroke: colors[si], "stroke-width": 2, "stroke-linejoin": "round" }, svg);
        if (n <= 40) se.values_cents.forEach((v, i) => s("circle", { cx: m.l + bw * i + bw / 2, cy: sy(v), r: 2.5, fill: colors[si] }, svg));
      });
    } else {
      const gap = Math.min(6, bw * 0.22);
      buckets.forEach((_, i) => {
        let pos = 0, neg = 0;
        series.forEach((se, si) => {
          const v = se.values_cents[i];
          if (!v) return;
          let y0, y1;
          if (v >= 0) { y0 = sy(pos); pos += v; y1 = sy(pos); } else { y1 = sy(neg); neg += v; y0 = sy(neg); }
          s("rect", { x: m.l + bw * i + gap / 2, y: Math.min(y0, y1), width: Math.max(1, bw - gap),
                      height: Math.max(0.5, Math.abs(y0 - y1)), fill: colors[si], rx: n > 45 ? 0 : 2 }, svg);
        });
      });
    }
    const hover = s("g", {}, svg);
    buckets.forEach((b, i) => {
      const r = s("rect", { x: m.l + bw * i, y: m.t, width: bw, height: H - m.t - m.b, fill: "transparent" }, hover);
      const on = e => {
        const rows = series.map((se, si) => ({ name: se.name, value: se.values_cents[i], color: colors[si] }))
          .filter(x => x.value !== 0);
        showTip(e, bucketLabel(b, true), rows, series.length > 1 ? (result.bucket_totals_cents || [])[i] : undefined);
      };
      r.addEventListener("pointermove", on);
      r.addEventListener("pointerdown", on);
      r.addEventListener("pointerleave", hideTip);
    });
    container.appendChild(svg);
    return { colors, series };
  }

  function horizontal(container, rows, opts) {
    // rows: [{name, value, color}] ; opts.diverging for +/- bars around zero
    const W = Math.max(320, container.clientWidth || 640);
    const labelW = Math.min(230, Math.max(110, W * 0.32));
    const rowH = 28, H = rows.length * rowH + 30;
    const m = { l: labelW, r: 70, t: 8, b: 22 };
    let lo = 0, hi = 0;
    rows.forEach(r => { lo = Math.min(lo, r.value); hi = Math.max(hi, r.value); });
    const nt = niceTicks(lo, hi, 4);
    const sx = v => m.l + (W - m.l - m.r) * ((v - nt.lo) / (nt.hi - nt.lo || 1));
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "Breakdown chart" });
    const g = s("g", { class: "grid" }, svg);
    nt.ticks.forEach(v => {
      s("line", { x1: sx(v), x2: sx(v), y1: m.t, y2: H - m.b, class: v === 0 ? "zero" : "" }, g);
      const t = s("text", { x: sx(v), y: H - 6, "text-anchor": "middle" }, svg);
      t.textContent = money(v, true);
    });
    rows.forEach((r, i) => {
      const y = m.t + i * rowH;
      const t = s("text", { x: m.l - 10, y: y + rowH / 2 + 4, "text-anchor": "end" }, svg);
      t.textContent = truncate(r.name, Math.floor(labelW / 7));
      const x0 = sx(Math.min(0, r.value)), x1 = sx(Math.max(0, r.value));
      const color = opts.diverging ? (r.value >= 0 ? "var(--up)" : "var(--down)") : r.color;
      const bar = s("rect", { x: x0, y: y + 5, width: Math.max(1, x1 - x0), height: rowH - 10, rx: 3, fill: color }, svg);
      const v = s("text", { x: r.value >= 0 ? x1 + 6 : x0 - 6, y: y + rowH / 2 + 4, "text-anchor": r.value >= 0 ? "start" : "end" }, svg);
      v.textContent = (opts.diverging && r.value > 0 ? "+" : "") + money(r.value, true);
      const on = e => showTip(e, r.name, [{ name: opts.valueLabel || "Cost", value: r.value, color }]);
      bar.addEventListener("pointermove", on);
      bar.addEventListener("pointerdown", on);
      bar.addEventListener("pointerleave", hideTip);
    });
    container.appendChild(svg);
  }

  function legend(container, series, colors) {
    if (!series || series.length < 2) return;
    const lg = document.createElement("div");
    lg.className = "legend";
    series.forEach((se, i) => {
      const it = document.createElement("span");
      const sw = document.createElement("i");
      sw.style.background = colors[i];
      it.appendChild(sw);
      it.appendChild(document.createTextNode(se.name));
      lg.appendChild(it);
    });
    container.appendChild(lg);
  }

  function render(container, result, chartType) {
    container.textContent = "";
    hideTip();
    const type = chartType || result.chart;
    const plot = document.createElement("div");
    plot.className = "chart-wrap";
    container.appendChild(plot);
    if (result.kind === "variance") {
      horizontal(plot, result.rows.map(r => ({ name: r.name, value: r.delta_cents })),
                 { diverging: true, valueLabel: "Change", label: "Change by " + result.group_label });
      return;
    }
    if (type === "table") { container.removeChild(plot); return; }
    if (type === "hbar" || result.buckets.length === 1) {
      horizontal(plot, result.series.map((se, i) => ({ name: se.name, value: Math.round(Number(se.total) * 100),
                                                     color: colorFor(i, se.key) })), { label: result.title });
      return;
    }
    const out = vertical(plot, result, type === "stacked" || type === "bar" || type === "line" ? type : "stacked");
    if (type !== "bar") legend(container, out.series, out.colors);
  }

  window.CostCharts = { render, money, bucketLabel, colorFor, hideTip };
})();
