// Chop editor grid math: plain functions, no DOM, so node can test them (tests/sim.js).
// The bar lines are the chopper's detected ones (analysis.json bars_s), so on a drifting song the
// bars are not all the same length. A beat is a quarter of whichever bar it sits in: approximate,
// since the chopper never detected beats inside a bar.
(function (root) {
  "use strict";

  // Bar lines covering 0..dur: the detected ones, extended before and after by the median bar.
  // Each line is { t, bar } where bar is the number the chopper prints (bars_s[i] starts bar i+1);
  // bars before the first detected line count down from 0.
  function barLines(bars, barlen, dur) {
    if (!bars || bars.length < 2) {
      const L = [];
      for (let k = 0, t = 0; t <= dur + 1e-9; k++, t = k * barlen) L.push({ t, bar: k + 1 });
      return L;
    }
    const out = [];
    let k = 0;
    for (let t = bars[0] - barlen; t > -barlen; t -= barlen) out.unshift({ t: Math.max(0, t), bar: k-- });
    bars.forEach((t, i) => out.push({ t, bar: i + 1 }));
    // the chopper ends bars_s at the song's end when the last bar is partial: that last entry is the
    // end, not a bar start (end: true; not counted, nothing after it)
    const last = out[out.length - 1];
    if (last.t >= dur - 1e-3) { last.end = true; return out; }
    for (let i = bars.length, t = bars[bars.length - 1] + barlen; t < dur - 1e-6; i++, t += barlen) out.push({ t, bar: i + 1 });
    return out;
  }

  // Beat times: 4 per bar, each a quarter of that bar. A partial bar (the one running into the song's
  // end, or the open one after the last line) keeps the median bar's beat spacing and gets only the
  // beats that fit, instead of four squeezed ones.
  function beats(lines, barlen, dur) {
    const out = [];
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].end) break;
      const t0 = lines[i].t, t1 = i + 1 < lines.length ? lines[i + 1].t : t0 + barlen;
      const partial = i + 1 >= lines.length || lines[i + 1].end;
      const step = (partial ? barlen : t1 - t0) / 4;
      for (let q = 0; q < 4; q++) { const t = t0 + q * step; if (t <= dur + 1e-9 && (q === 0 || t < t1 - 1e-9)) out.push(t); }
    }
    if (!out.length || out[out.length - 1] < dur) out.push(dur);
    return out;
  }

  // Nearest time in a sorted list
  function nearest(list, t) {
    let lo = 0, hi = list.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (list[m] <= t) lo = m; else hi = m; }
    return Math.abs(list[lo] - t) <= Math.abs(list[hi] - t) ? list[lo] : list[hi];
  }

  // mode: "beat" (default), "bar" (Shift), "free" (Alt). Clamped to the song.
  function snap(t, mode, lines, beatList, dur) {
    t = Math.max(0, Math.min(dur, t));
    if (mode === "free") return t;
    if (mode === "bar") return nearest(lines.map(l => l.t).concat([dur]), t);
    return nearest(beatList, t);
  }

  // Which bar a time sits in, and the beat inside it (1-4), for labels like "bar 23.2"
  function where(lines, t, barlen = 2) {
    let i = 0;
    while (i + 1 < lines.length && lines[i + 1].t <= t && !lines[i + 1].end) i++;
    // a partial bar counts its beats at the median spacing, like beats() does
    const t0 = lines[i].t, t1 = i + 1 < lines.length && !lines[i + 1].end ? lines[i + 1].t : t0 + barlen;
    return { bar: lines[i].bar, beat: 1 + Math.min(3, Math.floor(4 * (t - t0) / Math.max(1e-9, t1 - t0))) };
  }

  // round to tenths FIRST: 59.95 s must read 1:00.0, not 0:60.0
  const clock = s => { const d = Math.round(s * 10); return `${Math.floor(d / 600)}:${((d % 600) / 10).toFixed(1).padStart(4, "0")}`; };

  // The player's bank as 16 slots: pad 5 stays slot 4, empty pads stay empty (never compacted)
  function slots(pads) {
    const out = new Array(16).fill(null);
    for (const k in pads || {}) {
      const i = Number(k) - 1, e = pads[k];
      if (Number.isInteger(i) && i >= 0 && i < 16 && e && typeof e.file === "string") out[i] = e;
    }
    return out;
  }

  // The chopper's section map (analysis.json sections) as times. Section bars [a, b] run from
  // bars_s[a-1] to bars_s[b]. The first section also takes the pickup before bar 1, the last runs to
  // the song's end. Names are the chopper's guesses, kept with their "?"; "hook" reads as "chorus",
  // and detail in brackets is dropped from the label.
  function sections(secs, bars, dur) {
    if (!Array.isArray(secs) || !bars || bars.length < 2) return [];
    const out = [];
    for (const s of secs) {
      if (!s || !Array.isArray(s.bars)) continue;
      const [a, b] = s.bars;
      const t0 = bars[Math.max(0, a - 1)], t1 = b < bars.length ? bars[b] : dur;
      if (!(t1 > t0)) continue;
      const name = String(s.guess || "").replace(/\s*\(.*\)\s*$/, "").replace(/^hook/, "chorus") || "section";
      out.push({ t0, t1, name, letter: s.letter || "" });
    }
    if (out.length) { out[0].t0 = 0; out[out.length - 1].t1 = dur; }
    return out;
  }

  const api = { barLines, beats, nearest, snap, where, clock, slots, sections };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ChopMath = api;
})(this);
