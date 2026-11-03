// Beat-sync math for the two decks. Pure functions (no audio, no page), so test.sh can run them in node.
// Times are seconds. "bar" = one bar at playback rate 1; "real bar" = how long a bar lasts as heard.
"use strict";
(function (root) {
  const SyncMath = {
    // One bar of the pack at rate 1. loop_seconds / 8 is exact (every loop is conformed to 8 bars);
    // packs chopped before that only have the detected barlen.
    bar(meta, mul = 1) {
      return (meta.loop_seconds ? meta.loop_seconds / 8 : meta.barlen) / mul;
    },

    // How many leader bars one follower bar spans, as a power of 2 (0.5, 1, 2 ...), so a 77 BPM song
    // follows a 141 BPM one at half time instead of being sped up 83%.
    fold(followerBar, leaderRealBar) {
      return Math.pow(2, Math.round(Math.log2(followerBar / leaderRealBar)));
    },

    // The follower's playback rate that makes its bar exactly m leader bars long.
    solve(followerBar, leaderRealBar, m) {
      return followerBar / (m * leaderRealBar);
    },

    // The first bar line at or after t on a grid that starts at clock0. The 1e-6 keeps a time that sits
    // on a line from rounding up to the next one.
    lineAfter(clock0, step, t) {
      return clock0 + Math.ceil((t - clock0) / step - 1e-6) * step;
    },

    // New clock start after a rate change at `now`, so the bars played so far stay the same.
    reanchor(clock0, now, rOld, rNew) {
      return now - (now - clock0) * rOld / rNew;
    },

    // Position inside a loop (seconds at rate 1) for a deck whose loops all started at clock0.
    loopPos(clock0, t, r, dur) {
      return (((t - clock0) * r) % dur + dur) % dur;
    },

    // The bar a loop position is closest to, wrapped into the loop.
    nearestBar(pos, bar, nbars) {
      return Math.round(pos / bar) % nbars;
    },
  };
  if (typeof module !== "undefined" && module.exports) module.exports = SyncMath;
  else root.SyncMath = SyncMath;
})(typeof window !== "undefined" ? window : globalThis);
