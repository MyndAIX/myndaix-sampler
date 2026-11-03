// Runs the app's own script (app/index.html) in node against a fake audio clock and checks what the
// speakers would play: every sounding loop source on the right bar phase, nothing left sounding after
// a stop. Called by test.sh. Usage: node tests/sim.js <repo>
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");
const repo = process.argv[2];

// ---- fakes: just enough DOM and Web Audio for the page's script to run ----
class Param {
  constructor(v) { this.v0 = v; this.ev = []; }
  get value() { return this.ev.length ? this.ev[this.ev.length - 1][1] : this.v0; }
  set value(v) { if (this.ev.length) this.ev.push([-Infinity, v]); else this.v0 = v; }
  setValueAtTime(v, t) { this.ev.push([t, v]); }
  setTargetAtTime(v, t) { this.ev.push([t, v]); }
  at(t) { let v = this.v0; for (const [et, ev] of this.ev) if (et <= t) v = ev; return v; }
}
let audio;
class Src {
  constructor() { this.playbackRate = new Param(1); this.startT = null; this.stopT = Infinity; audio.srcs.push(this); }
  connect() {}
  // Web Audio: a start or stop time in the past means now; a later stop() call replaces the earlier one
  start(t = 0, off = 0) {
    if (this.startT !== null) throw new Error("start() called twice");   // Web Audio throws too
    this.startT = Math.max(t, audio.currentTime); this.off = off;
  }
  // once a source has ended, stop() changes nothing (no coming back to life)
  stop(t = 0) { if (audio.currentTime < this.stopT) this.stopT = Math.max(t, audio.currentTime); }
  sounding(T) { return this.startT !== null && this.startT <= T && T < this.stopT; }
  // seconds into the buffer at T, following every rate change since the start
  pos(T) {
    const ts = [this.startT, ...this.playbackRate.ev.map(e => e[0]).filter(t => t > this.startT && t < T), T];
    let p = this.off;
    for (let i = 0; i + 1 < ts.length; i++) p += (ts[i + 1] - ts[i]) * this.playbackRate.at(ts[i]);
    return p;
  }
}
class Ctx {
  constructor() { audio = this; this.currentTime = 0; this.srcs = []; this.state = "running"; this.destination = {}; }
  createGain() { return { gain: new Param(1), connect() {} }; }
  createBufferSource() { return new Src(); }
}
const el = () => new Proxy({ classList: { add() {}, remove() {} }, dataset: {}, style: {}, isConnected: false }, {
  get: (o, k) => k in o ? o[k] : (k === "querySelector" ? () => el() : k === "querySelectorAll" ? () => [] :
                                  typeof k === "string" ? (o[k] = () => {}) : undefined),
  set: (o, k, v) => { o[k] = v; return true; },
});
const listeners = {};
const sandbox = {
  AudioContext: Ctx, console, Math, Promise, URLSearchParams, Object, Number, String, JSON, Array, Set, WeakMap, Float32Array,
  document: { getElementById: () => el(), createElement: () => el(), documentElement: {},
              addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); } },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  requestAnimationFrame() {}, setTimeout() {}, clearTimeout() {},
  fetch: () => new Promise(() => {}),      // the page's start-up waits forever; the test drives it instead
  navigator: {}, location: { search: "" },
  localStorage: { getItem: () => null, setItem() {} },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(repo, "app/sync.js"), "utf8"), sandbox);
const html = fs.readFileSync(path.join(repo, "app/index.html"), "utf8");
const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
vm.runInContext(inline.join("\n"), sandbox);
const app = vm.runInContext("({ DECKS, S, cache, setPitch, resetPitch, syncTo, pressSync, setMul, stopLoops, toggleLoop, nativeBar, barSec })", sandbox);
const SM = sandbox.SyncMath;
const { DECKS, S } = app, L = DECKS.L, R = DECKS.R;

let bad = 0;
const check = (name, ok, info = "") => { console.log((ok ? "PASS  " : "FAIL  ") + name + (ok || !info ? "" : `  (${info})`)); if (!ok) bad = 1; };

const FILES = ["loops/a.wav", "loops/b.wav"];
function setup(loopL, loopR) {
  S.sync = null; audio.currentTime = 0; audio.srcs = [];
  for (const [d, sec] of [[L, loopL], [R, loopR]]) {
    stop(d);
    Object.assign(d, { pack: d.id + sec, meta: { loop_seconds: sec, barlen: sec / 8, files: FILES }, banks: { loops: FILES },
                       bank: "loops", semis: 0, fine: 1, mul: 1 });
    for (const f of FILES) app.cache[d.pack + "/" + f] = { duration: sec };
  }
}
const at = t => { audio.currentTime = t; };
const stop = d => app.stopLoops(d);
const loop = (d, f = FILES[0]) => app.toggleLoop(d, f);
const sounding = (d, T) => audio.srcs.filter(s => s.sounding(T) && s.deck === d.id);
// tag each source with its deck as it is made (startLoop connects it to that deck's volume node)
const origCreate = Ctx.prototype.createBufferSource;
Ctx.prototype.createBufferSource = function () {
  const s = origCreate.call(this);
  s.connect = node => { s.deck = node === L.volGain ? "L" : node === R.volGain ? "R" : "?"; };
  return s;
};

// Bar phase of each deck at T in the finer of the two grids (follower bar = m leader bars), as 0..1.
function phases(T) {
  const m = S.sync.m, F = DECKS[S.sync.follower], Ld = DECKS[S.sync.leader], u = Math.min(1, m);
  const ph = (d, scale) => sounding(d, T).map(s => { const x = s.pos(T) / app.nativeBar(d) * scale; return x - Math.floor(x); });
  return { lead: ph(Ld, 1 / u), follow: ph(F, m / u) };
}
const close = (a, b) => { const d = Math.abs(a - b) % 1; return Math.min(d, 1 - d) < 1e-6; };
function locked(name, T, wantFollow = 1) {
  const { lead, follow } = phases(T);
  const ok = lead.length > 0 && follow.length === wantFollow && [...lead, ...follow].every(p => close(p, lead[0]));
  check(name, ok, `leader ${lead.map(x => x.toFixed(4))} follower ${follow.map(x => x.toFixed(4))}`);
}

// 1. the done-test: Benga (1.71828 s bars) on L, Postcard (3.13469 s) on R
setup(8 * 1.71828, 8 * 3.13469);
at(0.4); loop(L);
at(3.1); app.syncTo(R);
at(3.3); loop(R);
locked("Postcard synced to Benga: on Benga's bar lines", 6);
locked("... still locked 32 Benga bars later", 3.3 + 32 * 1.71828 + 0.37);
at(20.21); app.setPitch(L, 2);
locked("... and after a +2 pitch change on Benga", 47.9);
check("the follower took the new tempo", Math.abs(app.barSec(R) - 2 * app.barSec(L)) < 1e-9);

// 2. both playing when SYNC is pressed: the follower restarts on the leader's next bar line
setup(16, 16);
at(0); loop(L);
at(0.3); loop(R);
at(1); app.syncTo(R);
locked("both playing, SYNC: locked after the restart", 2.5);
check("... the old follower sound plays until the bar line, then stops",
      sounding(R, 1.9).length === 1 && sounding(R, 2.1).length === 1 && sounding(R, 2.1)[0] !== sounding(R, 1.9)[0]);

// 3. review P1: a pitch change while the restart is still waiting for its bar line
setup(16, 16);
at(0); loop(L);
at(0.3); loop(R);
at(1); app.syncTo(R);
at(1.5); app.setPitch(L, 12);
locked("pitch change before a pending restart: still locked", 3);
locked("... and later", 9.1);

// 4. review P1: stop and load reach the sound still playing out to the bar line
setup(16, 16);
at(0); loop(L);
at(0.3); loop(R);
at(1); app.syncTo(R);
at(1.5); stop(R);
check("stop during a sync handover silences every source", sounding(R, 1.6).length === 0 && sounding(R, 3).length === 0);
setup(16, 16);
at(0); loop(L); loop(L, FILES[1]);
at(0.5); loop(L);          // stops on the bar at 2
at(1); stop(L);
check("stop reaches a loop that was stopping on the next bar", sounding(L, 1.5).length === 0);

// review #2: a loop switched on and off before its bar line, then the leader slows down
setup(16, 16);
at(0); loop(L);
at(0.1); app.syncTo(R);
at(0.5); loop(R);
at(0.6); loop(R);
at(1); app.setPitch(L, -12);
check("a loop cancelled before its bar line never sounds, even after a speed change",
      audio.srcs.filter(s => s.deck === "R").every(s => !s.sounding(2.5) && !s.sounding(3)));

// review #3: a loop queued for the next bar, then SYNC restarts the deck a bar later: the queued
// loop plays until the restart, and keeps its place through a speed change while it waits
setup(16, 16);
at(0); loop(L);
at(0.3); loop(R);
at(1); loop(R, FILES[1]);          // queued for R's bar line at 2.32
at(2.2); app.syncTo(R);            // restart on L's line at 4.02
check("a loop queued before a SYNC restart plays until the restart",
      sounding(R, 3).length === 2 && sounding(R, 3).some(s => s.file === FILES[1]));
setup(16, 16);
at(0); loop(L);
at(0.3); loop(R);
at(1); loop(R, FILES[1]);
at(2.2); app.syncTo(R);
at(2.25); app.setPitch(L, -12);    // before the queued loop starts
const q = audio.srcs.filter(s => s.deck === "R" && s.file === FILES[1] && s.startT < s.stopT && s.stopT < Infinity);
const lead = sounding(L, 2.5)[0];
check("... and through a slowdown before it starts: on L's beat, stops on the restart line",
      q.length === 1 && close(q[0].pos(q[0].startT + 0.5) / 2 % 1, lead.pos(q[0].startT + 0.5) / 2 % 1)
      && Math.abs(q[0].stopT - sounding(R, q[0].stopT + 0.01).find(s => s.file === FILES[1]).startT) < 1e-9);

// 5. review P1: ×2/÷2 that leaves the grids between each other's bar lines
setup(16, 32);             // L 2 s bars, R 4 s bars (half time under L)
at(0); loop(L);
at(0.1); app.syncTo(R);
at(1.2); loop(R);          // joins on L's line at 2: R bars every 4 s from 2
at(5); app.setMul(L, 0.5); // L now 4 s bars from 0: half a bar off R's
locked("÷2 on the leader: the follower moves back onto its bar lines", 12);

// 6. an idle leader joins a playing follower on its bar lines
setup(16, 32);
app.syncTo(R);
at(0.7); loop(R);
at(2.2); loop(L);
locked("idle leader starts on the playing follower's bar lines", 10);

// 7. letting go
setup(16, 16);
app.syncTo(R); at(1); app.setPitch(R, R.semis + 1);
check("the follower's own pitch key turns sync off", S.sync === null);
app.syncTo(R); app.pressSync(L);
check("SYNC on the leader swaps: L follows R", S.sync && S.sync.follower === "L");
app.resetPitch(L);
check("reset on the follower: sync off, speed back to 1", S.sync === null && L.fine === 1 && L.semis === 0);

// 8. bank keys: Shift + the deck's top pad row (a Mac sends F-keys as brightness etc. unless fn is held)
setup(16, 16);
Object.assign(L.banks, { synth16: FILES, mix16: FILES, vocal16: FILES }); Object.assign(R.banks, L.banks);
const press = (key, code, shiftKey = false) => {
  let blocked = false;
  for (const fn of listeners.keydown) fn({ key, code, shiftKey, target: { tagName: "BODY" }, preventDefault() { blocked = true; } });
  return blocked;
};
press("@", "Digit2", true);
check("Shift+2 picks the left deck's 2nd bank", L.bank === "synth16" && R.bank === "loops");
press(")", "Digit0", true);
check("Shift+0 picks the right deck's 4th bank", R.bank === "vocal16" && L.bank === "synth16");
press("F3", "F3");
check("fn+F3 still picks the left deck's 3rd bank", L.bank === "mix16");
const PADS = Array.from({ length: 16 }, (_, i) => `mix16/p${i}.wav`);
L.banks.mix16 = PADS; for (const f of PADS) app.cache[L.pack + "/" + f] = { duration: 1 };
app.S.hits.length = 0; const hits = 0;   // the page keeps only the last 20 hits
press("1", "Digit1");
check("plain 1 still plays pad 13, not a bank", L.bank === "mix16" && app.S.hits.length === hits + 1
      && app.S.hits[app.S.hits.length - 1].pad === 13);

process.exit(bad);
