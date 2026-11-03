# Chop editor: design (2026-11-03)

## What and why
Jefe, 2026-11-03: "a chain tool that loads the whole wave sample for each stem, then it shows what bars
are being chopped then the user can listen and arrange or select what bars to chop or sample vs where
the AI decides all that." The AI chopper picks the chops on its own today. On Postcard it keeps
landing on "send me a postcard" because the loudest breath-gap phrases are the hook. The editor puts
the choice in the player's hands. The AI's picks stay visible as suggestions.

**Done-test (Jefe checks it himself):** open Postcard, pick the vocal stem, see the whole song with bar
numbers, drag over "how can I make you understand", hear it, drop it on pad 5, and play it from the
keyboard on a deck (bank **mine**). Reload, and it's still there. Re-chop the pack with `--force`, and
it's still there.

## Prior art (from the products' published manuals; no web check this session)
| Product | What it does | Verdict |
|---|---|---|
| MPC Chop mode (Akai) | Whole sample on one wave. Regions are set by threshold, a regular grid or by hand, then assigned to pads. | BORROW: one long wave, regions, then pads |
| Maschine Sample Editor, Slice tab (NI) | Slices by grid, transient or by hand; drag a slice to a pad. Jefe owns the MK2. | BORROW: drag a region to a pad |
| Serato Sample | The AI places 16 cue points, and the user drags them. | BORROW: AI suggestions the user overrides |
| Ableton Simpler, Slice mode | Slices by beat, bar or transient. | Same pattern, nothing new |
| wavesurfer.js regions plugin | A ready-made web wave plus regions. | REJECT for v1: a new dependency (Jefe's call). The app already draws waves on a canvas (`drawDeck`). |

Not built here: transient slicing, time-stretch, a sample-start knob, and per-pad envelopes.

## Data flow
1. **Stems served read-only.** New route `/stems/<slug>/<name>.wav`, where name is one of
   drums, bass, other, guitar, piano, vocals, mix. mix maps to `songs/<slug>.wav`. It uses the same
   `inside()` containment as `/packs/`, and any other name gets a 404. A stem is about 28 MB
   (44.1 kHz stereo 16-bit, about 2:40). Only the chosen stem is fetched and decoded.
2. **Editor page `app/chop.html`.** It is separate from `index.html` (680 lines already) and shares
   the same styling.
   - Pack menu, stem tabs, and one wide canvas with the whole song.
   - Bar lines and numbers come from `analysis.json` `bars_s`.
   - Zoom and scroll on the wave.
   - The AI's picks are drawn as shaded regions labelled with their bank and pad.
   - Drag to select. The selection snaps to the nearest beat by default, to the bar with Shift, or
     is free with Alt. Beats are bar/4, interpolated between the detected bar lines, so they follow a
     drifting song.
   - Space plays the selection once; L loops it.
   - A 16-pad grid shows bank **mine**. Drop the selection on a pad, or press its key. Click a filled
     pad to hear it; the clear button empties it.
3. **Save.** `POST /api/mine` with `{slug, pad 1-16, stem, t0, t1}`, or `{slug, pad, clear: true}`.
   The server checks:
   - slug is a real pack;
   - stem is in the list;
   - 0 ≤ t0 < t1 ≤ the stem's length;
   - t1 − t0 is between 0.05 and 32 s;
   - pad is between 1 and 16.

   It then cuts the WAV from the full-rate stem with the stdlib `wave` and `array` modules (no new
   dependency; serve.py stays stdlib-only), with a 3 ms fade in and out against clicks. The cut is
   never stretched (vocal rule, `vocal-chops-natural`). It writes `mine NN.wav` and `regions.json`
   atomically (temp file, then rename) under one lock per slug.
4. **Storage, outside the pack:** `$SAMPLER_HOME/mine/<slug>/regions.json` plus `mine 01.wav` …
   `mine 16.wav`.
   - `regions.json` is the record; the WAVs are rebuilt from it. It is
     `{"source": <songs/<slug>.source>, "pads": {"5": {"stem": "vocals", "t0": 71.42, "t1": 74.9}}}`.
   - flippack `--force` renames `packs/<slug>` only, so `mine/` survives a re-chop.
   - When a slug is handed to a *different* song (`choose_slug` with `--force`), `mine/<slug>` is
     renamed to `.prev-<stamp>` along with the audio, stems and pack, so one song's chops never land
     on another song.
   - The server also refuses to list mine for a slug whose `source` doesn't match.
5. **Decks load bank mine.**
   - `/api/mine/<slug>` returns the filled pads. `/mine/<slug>/…` serves the WAVs read-only.
   - The pack loader adds a sparse bank named `mine`, so pad 5 stays pad 5 and empty pads stay
     empty. It shows after the existing banks and is selected with the same bank keys.
   - A save makes the server queue a `mine <slug>` command on the existing `/control` queue. An open
     deck playing that pack reloads bank mine without a page reload.
6. **AI picks on the wave.** flippack writes `regions` to `analysis.json`: per chop file, its stem
   and its t0/t1 in seconds. Loops already carry `start`/`seconds`. Packs built before this have no
   `regions`; they show loops only until re-chopped. Loops come out the same; the 3 packs are
   re-chopped from their stems in about 30 s each.
7. **AI parity.**
   - `vs.py chop <slug> <pad> <stem> <t0> <t1|+bars>` posts the same request as the editor.
   - `vs.py chop <slug> <pad> clear` empties a pad.
   - `vs.py mine <slug>` lists the pads.
   - The AI can fill the bank for Jefe ("put 'how can I make you' on pad 5"), and everything shows
     on the wave.

## Edge cases and failure modes
| Case | Handling |
|---|---|
| Re-chop with `--force` | mine/ is outside packs/ and is untouched. test.sh proves it. |
| Slug taken by another song | mine/ is renamed with the rest, and the source check refuses a mismatch. |
| Stems missing (pack copied without stems) | The editor says so. The deck still plays any existing mine WAVs. |
| Drive unplugged mid-save | Temp file then rename: either the old pad or the new one, never half a WAV. |
| Two saves at once (editor and vs.py) | One lock per slug. The last write wins, and the pad is whole either way. |
| Span past the end, t0 ≥ t1, NaN, huge | Rejected with 400 and the reason. |
| A 32 s span decoded on the deck | About 11 MB float; fine. Longer is refused. |
| A pad overwritten by mistake | The old WAV for that pad moves to `mine/<slug>/.trash/` (never deleted). |
| analysis.json without `bars_s` | The grid falls back to `barlen` from `first_drum_bar_s`. |

## Security surface
- Untrusted input: the POST body (slug, stem, numbers) and the URL paths.
  - slug must match an existing pack directory name (the same check `load` uses).
  - stem must be in the allowlist.
  - numbers are parsed as floats and checked to be finite and in range.
  - pad is an int from 1 to 16.
- File names are built only from the pad number (`mine 05.wav`), never from user text.
- Reads go through `inside()`, which resolves symlinks.
- The existing guards cover the new routes: the Host check (127.0.0.1 or localhost only) and the
  `X-VS` header required on POSTs, so another website can't POST to it.
- Nothing from regions.json is put into HTML except through `textContent`.

## Fast loop
- `test.sh`:
  - `/api/mine` rejects bad slugs, stems, numbers, pads and traversal.
  - A valid save writes a WAV whose length is exactly round((t1−t0)·sr) frames, with zeroed edges.
  - A re-save keeps the old WAV in `.trash/`.
  - A flippack `--force` on the fixture leaves `mine/` byte-identical.
  - A slug handed to another song moves `mine/`.
  - `/stems/` serves only the allowlist.
- `tests/sim.js`: snapping to beat, bar or free on a drifting grid; the sparse mine bank keeps pad
  positions.
- Headless (Playwright): the done-test end to end on Postcard. Jefe's ear is last.

## Deliberately not built (v1)
- A lyrics lane: whisper word timing. whisper is on Jefe's Mac, but adding it to the friends' setup
  is a new dependency, his call.
- More than one user bank, renaming pads, and exporting mine to Maschine (it's WAV files already, so
  it can be dragged in).
- Editing the AI's own banks in place: they stay as made, and mine is the player's.
