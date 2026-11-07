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
  async decodeAudioData(ab) { return { duration: 1, from: ab }; }
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
sandbox.addEventListener = () => {};   // window focus: the test calls refreshMine itself
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(repo, "app/sync.js"), "utf8"), sandbox);
vm.runInContext(fs.readFileSync(path.join(repo, "app/chopmath.js"), "utf8"), sandbox);
const html = fs.readFileSync(path.join(repo, "app/index.html"), "utf8");
const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
vm.runInContext(inline.join("\n"), sandbox);
const app = vm.runInContext("({ DECKS, S, cache, setPitch, resetPitch, syncTo, pressSync, setMul, stopLoops, toggleLoop, nativeBar, barSec, refreshMine, setBank })", sandbox);
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
check("Shift+2 picks the left deck's 2nd bank (and the browser doesn't act on it)",
      press("@", "Digit2", true) && L.bank === "synth16" && R.bank === "loops");
check("Shift+0 picks the right deck's 4th bank", press(")", "Digit0", true) && R.bank === "vocal16" && L.bank === "synth16");
check("fn+F3 still picks the left deck's 3rd bank", press("F3", "F3") && L.bank === "mix16");
const PADS = Array.from({ length: 16 }, (_, i) => `mix16/p${i}.wav`);
L.banks.mix16 = PADS; for (const f of PADS) app.cache[L.pack + "/" + f] = { duration: 1 };
app.S.hits.length = 0; const hits = 0;   // the page keeps only the last 20 hits
press("1", "Digit1");
check("plain 1 still plays pad 13, not a bank", L.bank === "mix16" && app.S.hits.length === hits + 1
      && app.S.hits[app.S.hits.length - 1].pad === 13);
// AZERTY: the unshifted digit row types & é " ' and Shift types the digits
check("AZERTY: unshifted & (the 1 key) plays pad 13", press("&", "Digit1") && L.bank === "mix16"
      && app.S.hits.length === hits + 2 && app.S.hits[app.S.hits.length - 1].pad === 13);
check("AZERTY: Shift + the 1 key (types 1) picks bank 1, not a pad",
      press("1", "Digit1", true) && L.bank === "loops" && app.S.hits.length === hits + 2);

// ---- chop editor grid math (app/chopmath.js) ----
const CM = sandbox.ChopMath;
{
  // a drifting grid: bars 2.0, 2.1, 1.9 s long, first line at 0.5 s, song ends 0.7 s into a partial
  // bar. Shaped like flippack's bars_s: its last entry is the song's end (Postcard: 158.59, 160.99)
  const bars = [0.5, 2.5, 4.6, 6.5, 7.2], dur = 7.2, lines = CM.barLines(bars, 2, dur);
  check("bar lines: a pickup bar 0 before the first detected line, then 1..4 on the detected times",
        lines[0].bar === 0 && lines[0].t === 0 && lines.slice(1).map(l => l.t).join() === bars.join()
        && lines.slice(1, 5).map(l => l.bar).join() === "1,2,3,4");
  check("... the song's end closes the grid: marked end, not a bar", lines[lines.length - 1].end === true && lines.length === 6);
  const open = CM.barLines([0.5, 2.5], 2, 7.2);
  check("no end marker: the grid runs on at the median bar", open.map(l => l.t).join() === "0,0.5,2.5,4.5,6.5");
  const bt = CM.beats(lines, 2, dur);
  check("beats are quarters of whichever bar they sit in", bt.includes(2.5 + 2.1 / 4) && bt.includes(4.6 + 1.9 / 2));
  check("the partial last bar keeps the median beat spacing: only the beats that fit, plus the song's end",
        bt.filter(t => t >= 6.5).join() === [6.5, 7, 7.2].join());
  const ob = CM.beats(open, 2, 7.2);
  check("... and the open bar after the last line too", ob.filter(t => t >= 6.5).join() === [6.5, 7, 7.2].join());
  check("snap to beat on a drifting bar", Math.abs(CM.snap(2.5 + 2.1 / 4 + 0.05, "beat", lines, bt, dur) - (2.5 + 2.1 / 4)) < 1e-9);
  check("Shift snaps to the bar line", CM.snap(4.0, "bar", lines, bt, dur) === 4.6);
  check("Alt is free, clamped to the song", CM.snap(3.333, "free", lines, bt, dur) === 3.333 && CM.snap(99, "free", lines, bt, dur) === dur);
  check("the song's end is a snap target", CM.snap(7.15, "bar", lines, bt, dur) === dur);
  const w = CM.where(lines, 4.6 + 1.9 * 0.6);
  check("where: bar 3, beat 3", w.bar === 3 && w.beat === 3);
  check("where in the partial last bar: bar 4, beat 2 at the median spacing", CM.where(lines, 7.1, 2).bar === 4 && CM.where(lines, 7.1, 2).beat === 2);
  check("clock reads m:ss.s", CM.clock(83.25) === "1:23.3" && CM.clock(5) === "0:05.0"
      && CM.clock(59.95) === "1:00.0" && CM.clock(119.97) === "2:00.0");
  const sl = CM.slots({ "5": { file: "a.wav" }, "16": { file: "b.wav" }, "17": { file: "c.wav" }, "0": { file: "d.wav" }, x: { file: "e" }, "3": { nofile: 1 } });
  check("slots keep pad numbers in place, never compact, drop bad keys",
        sl.length === 16 && sl[4].file === "a.wav" && sl[15].file === "b.wav" && sl.filter(Boolean).length === 2);
  const sec = CM.sections([{ bars: [1, 2], guess: "intro", letter: "A" }, { bars: [3, 4], guess: "hook?", letter: "B" },
                           { bars: [5, 5], guess: "verse? (light drums)", letter: "C" }], [0.5, 2.5, 4.6, 6.5, 7.2], 9);
  check("sections: bar spans become times, the first takes the pickup, the last runs to the end",
        sec.map(s => `${s.t0}-${s.t1}`).join() === "0-4.6,4.6-7.2,7.2-9");
  check("... hook reads as chorus, bracketed detail dropped, the ? kept",
        sec.map(s => s.name).join() === "intro,chorus?,verse?");
  check("... no section map: nothing to show", CM.sections(undefined, [0, 2], 4).length === 0);
}

// ---- the player's bank on a deck ("mine") ----
// a fetch the test answers by hand, so it can make an older bank arrive after a newer one
const pend = [];
sandbox.fetch = url => new Promise(res => pend.push({ url, res }));
const answer = async (match, body) => {
  const i = pend.findIndex(p => p.url.includes(match));
  if (i < 0) throw new Error("no fetch waiting for " + match + ": " + pend.map(p => p.url).join(" "));
  const [p] = pend.splice(i, 1);
  p.res({ json: async () => body, arrayBuffer: async () => p.url });
  for (let k = 0; k < 20; k++) await null;     // let the page's awaits run
};
const bank = (rev, pads) => ({ slug: "postcard", rev, pads });
(async () => {
  setup(16, 16);
  L.pack = "postcard"; L.banks = { loops: FILES }; L.bank = "loops";
  for (const f of FILES) app.cache["postcard/" + f] = { duration: 16 };
  at(0); loop(L);
  let r = app.refreshMine(L);
  await answer("/api/mine/postcard", bank(1, { "5": { file: "p05-r0001-aaaaaa.wav", stem: "vocals", a: 441000, b: 500000, sr: 44100 } }));
  await answer("/mine/postcard/p05-r0001-aaaaaa.wav", null); await r;
  check("mine: pad 5 holds the cut, the other 15 pads stay empty",
        L.banks.mine && L.banks.mine.length === 16 && L.banks.mine[4] === "mine/p05-r0001-aaaaaa.wav" && L.banks.mine.filter(Boolean).length === 1);
  check("... labelled with its stem and where it sits in the song", L.labels["mine/p05-r0001-aaaaaa.wav"] === "vocals 0:10.0");
  check("... and the loop that was playing keeps playing on its bank", L.bank === "loops" && sounding(L, 0.5).length === 1);

  // two refreshes, the newer one answered first: the older one must not land on top
  const r1 = app.refreshMine(L), r2 = app.refreshMine(L);
  const reqs = pend.filter(p => p.url.includes("/api/mine/")).length;
  pend.reverse();                                  // answer() takes the first match: the newer request
  await answer("/api/mine/postcard", bank(3, { "6": { file: "p06-r0003-cccccc.wav", stem: "drums", a: 0, b: 44100, sr: 44100 } }));
  await answer("/mine/postcard/p06-r0003-cccccc.wav", null);
  await answer("/api/mine/postcard", bank(2, { "7": { file: "p07-r0002-bbbbbb.wav", stem: "bass", a: 0, b: 44100, sr: 44100 } }));
  await r1; await r2;
  check("two refreshes out of order: the newest bank wins", reqs === 2 && L.banks.mine[5] === "mine/p06-r0003-cccccc.wav" && !L.banks.mine[6]);
  check("... and the stale one never fetched its audio", !pend.some(p => p.url.includes("p07")));
  check("cuts no deck uses any more are let go from the cache",
        !app.cache["postcard/mine/p05-r0001-aaaaaa.wav"] && app.cache["postcard/mine/p06-r0003-cccccc.wav"]);

  // a pack load while a bank fetch is still out: the old pack's bank must not land on the new pack
  r = app.refreshMine(L);
  L.mineGen++; L.pack = "other";                 // what loadPack does first
  await answer("/api/mine/postcard", bank(4, { "1": { file: "p01-r0004-dddddd.wav", stem: "vocals", a: 0, b: 44100, sr: 44100 } }));
  await r;
  check("a pack load overtakes a running bank fetch", !pend.some(p => p.url.includes("p01-r0004")));
  L.pack = "postcard";

  // a refresh overtaken while its audio decodes adds nothing to the shared cache
  r = app.refreshMine(L);
  await answer("/api/mine/postcard", bank(6, { "2": { file: "p02-r0006-eeeeee.wav", stem: "vocals", a: 0, b: 44100, sr: 44100 } }));
  const r3 = app.refreshMine(L);
  await answer("/mine/postcard/p02-r0006-eeeeee.wav", null); await r;
  check("an overtaken refresh leaves nothing in the cache", !app.cache["postcard/mine/p02-r0006-eeeeee.wav"]);
  await answer("/api/mine/postcard", bank(7, {})); await r3;

  // two decks on one pack: the left waits on its second cut while the right installs an empty bank
  // and lets go of the first cut; the left must still end up with both buffers
  R.pack = "postcard"; R.banks = { loops: FILES }; R.bank = "loops"; R.labels = {};
  r = app.refreshMine(R);
  await answer("/api/mine/postcard", bank(8, { "1": { file: "p01-r0008-xxxxxx.wav", stem: "vocals", a: 0, b: 44100, sr: 44100 } }));
  await answer("/mine/postcard/p01-r0008-xxxxxx.wav", null); await r;
  const XY = { "1": { file: "p01-r0008-xxxxxx.wav", stem: "vocals", a: 0, b: 44100, sr: 44100 },
               "2": { file: "p02-r0009-yyyyyy.wav", stem: "drums", a: 0, b: 44100, sr: 44100 } };
  const rl = app.refreshMine(L);
  await answer("/api/mine/postcard", bank(9, XY));            // X is cached: held; Y is fetched
  const rr = app.refreshMine(R);
  await answer("/api/mine/postcard", bank(10, {})); await rr;  // the right lets go of X
  await answer("/mine/postcard/p02-r0009-yyyyyy.wav", null); await rl;
  check("two decks: a filled pad never loses its buffer to the other deck's cleanup",
        L.banks.mine[0] && app.cache["postcard/mine/p01-r0008-xxxxxx.wav"] && app.cache["postcard/mine/p02-r0009-yyyyyy.wav"]);
  delete R.banks.mine; R.pack = null;

  // keys: Shift+5 picks the left deck's mine bank, Shift+6 the right's, plain 5 / 6 are still SYNC
  L.banks.mine = new Array(16).fill(undefined); L.banks.mine[0] = "mine/x.wav"; L.bank = "loops";
  R.banks.mine = new Array(16).fill(undefined); R.banks.mine[0] = "mine/y.wav"; R.bank = "loops";
  check("Shift+5 picks the left deck's mine bank", press("%", "Digit5", true) && L.bank === "mine" && R.bank === "loops");
  check("Shift+6 picks the right deck's mine bank", press("^", "Digit6", true) && R.bank === "mine");
  S.sync = null; press("6", "Digit6");
  check("plain 6 is still SYNC on the right deck", S.sync && S.sync.follower === "R");
  delete R.banks.mine; R.bank = "loops";
  press("^", "Digit6", true);
  check("Shift+6 with no mine bank does nothing", R.bank === "loops");

  // an empty bank (every pad cleared) takes the bank away and moves a deck that was on it
  L.bank = "mine";
  r = app.refreshMine(L);
  await answer("/api/mine/postcard", bank(5, {})); await r;
  check("an empty bank: gone, and the deck moves back to a bank it has", !L.banks.mine && L.bank === "loops");
  process.exit(bad);
})().catch(e => { console.log("FAIL  sim crashed: " + e.stack); process.exit(1); });
