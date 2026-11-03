# Chop editor: design (2026-11-03, rev 2 after KilaBz review b092ba05)

## What and why
Jefe, 2026-11-03: "a chain tool that loads the whole wave sample for each stem, then it shows what bars
are being chopped then the user can listen and arrange or select what bars to chop or sample vs where
the AI decides all that." The AI chopper picks the chops on its own today. Jefe hears Postcard keep
landing on "send me a postcard". The editor puts the choice in the player's hands. The AI's picks stay
visible as suggestions.

**Done-test (Jefe checks it himself):**
1. Open Postcard, pick the vocal stem, and see the whole song with bar numbers.
2. Drag over "how can I make you understand" and hear it.
3. Drop it on pad 5 and play it from the keyboard on a deck (bank **mine**, Shift+5 on the left
   deck).
4. Reload, and it's still there.
5. Re-chop the pack with `--force`, and it's still there.

## Prior art (from the products' published manuals, not checked on the web this session)
| Product | What it does | Verdict |
|---|---|---|
| MPC Chop mode (Akai) | Whole sample on one wave. Regions are set by threshold, a regular grid or by hand, then assigned to pads. | BORROW: one long wave, regions, then pads |
| Maschine Sample Editor, Slice tab (NI) | Slices by grid, transient or by hand; drag a slice to a pad. Jefe owns the MK2. | BORROW: drag a region to a pad |
| Serato Sample | The AI places 16 cue points, and the user drags them. | BORROW: AI suggestions the user overrides |
| Ableton Simpler, Slice mode | Slices by beat, bar or transient. | Same pattern, nothing new |
| wavesurfer.js regions plugin | A ready-made web wave plus regions. | REJECT for v1: a new dependency (Jefe's call). The app already draws waves on a canvas (`drawDeck`). |

Not built here: transient slicing, time-stretch, a sample-start knob, and per-pad envelopes.

## Data flow
1. **One server validator, `pack_paths(slug)`**, used by every new route. It requires:
   - the slug matches `^[a-z0-9][a-z0-9-]{0,79}$` and contains no `.prev-`;
   - `packs/<slug>/analysis.json` exists;
   - Path checks only. Whether a save or mine playback is allowed is a separate check,
     `owner_ok(slug, owner)`. It requires `songs/<slug>.source` to exist and to equal the given
     owner; a missing marker never matches. Without a marker the wave can be viewed, but the pack
     can't be saved to and its mine bank isn't listed. A known mismatch is always refused.
   - The resolved pack, stem, song and mine paths each stay inside their own root (`inside()`, which
     resolves symlinks).

   The validator returns the paths and the owner string. Stems map explicitly:
   `drums|bass|other|guitar|piano|vocals` go to `stems/htdemucs_6s/<slug>/<name>.wav`, and `mix` goes
   to `songs/<slug>.wav`.
2. **Read-only GET routes.** Each has an exact shape, no directory listings, and HEAD behaves the same.
   - `/stems/<slug>/<name>.wav` returns the stem.
   - `/api/mine/<slug>` returns:
     - `{owner, rev, pads: {"5": {file, stem, a, b, sr}}, formats}`, where `a` and `b` are the
       sample frames the cut used;
     - `formats` reports, per stem, whether it can be saved from (16-bit PCM only in v1).
   - `/mine/<slug>/<file>` serves only a file named in the current manifest. Anything else is a 404,
     so archives, `.trash` and old revisions are never served.
3. **Editor page `app/chop.html`.** It is separate from `index.html` (680 lines already) and shares
   the same styling.
   - Pack menu, stem tabs, and one wide canvas with the whole song.
   - Bar lines and numbers come from `analysis.json` `bars_s`. The last interval may be a partial
     bar ending at the song's end, and is drawn as partial.
   - Zoom and scroll on the wave.
   - The AI's picks are drawn as shaded regions labelled with their bank and pad.
   - Drag to select. The selection snaps to the nearest beat by default, to the bar with Shift, or
     is free with Alt. Beats are bar/4, interpolated between the detected bar lines; the UI labels
     them as approximate on a drifting song.
   - Space plays the selection once; L loops it.
   - A 16-pad grid shows bank **mine**. Drop the selection on a pad, or press its key. Click a filled
     pad to hear it; the clear button empties it.
   - A stem that isn't 16-bit PCM shows "can't save from this stem (format)", and the wave still
     displays.
4. **Save: `POST /api/mine`** with `{slug, owner, pad 1-16, stem, t0, t1}`, or `{slug, owner, pad,
   clear: true}`.
   - **Guards and checks.** The request goes through `read_json()`, so the existing Host, exact
     `X-VS`, Origin and body-size guards apply. Then:
     - the slug passes `pack_paths`;
     - `owner` equals the current `.source` (this is the token the editor got when it opened);
     - stem is in the list;
     - t0 and t1 are finite, with 0 ≤ t0 < t1 ≤ the stem's length;
     - t1 − t0 is between 0.05 and 32 s;
     - pad is an int from 1 to 16.
   - **Lock.** The server takes the chopper's own slug lock: `songs/.<slug>.lock`, flock, non-blocking,
     on a fresh fd. If a chop holds it, the reply is `409 busy, a chop is running`, and no HTTP thread
     waits. Under the lock, it rechecks `owner == .source` (409 if not) and holds the lock through the
     commit. The global `/control` lock is never held while cutting.
   - **Cut.** The cut comes from the full-rate stem with stdlib `wave` and `array('h')`: PCM16 only,
     with explicit channels and little-endian byte order. It uses frames `a = round(t0·sr)` and
     `b = round(t1·sr)`, so the frame count is `b − a`, with a 3 ms linear fade at each end.
     Nothing is stretched. Deck pitch and sync still play it faster or slower like every other pad,
     the classic sampler behavior.
   - **Commit.**
     - The cut is written to a new name, created exclusively (`O_EXCL`):
       `mine/<slug>/p05-r0007-<6 random hex>.wav`. An orphan left by a crash before the commit is
       never overwritten or reused, and stays on disk.
     - That file is fsynced.
     - The new manifest goes to `regions.json.tmp`, is fsynced, then renamed onto `regions.json`,
       and the directory is fsynced.
     - That rename is the commit. Before it, the old manifest and old audio are untouched; after
       it, the new pad is whole.
     - Old WAVs are never deleted (never-delete rule). Each manifest records the previous revision's
       pad entry, so a clear or overwrite can be undone by hand. The undo button is v2.
     - There's no promise beyond what fsync gives on an external drive.
   - After the commit, a `mine <slug> <rev>` hint goes on the `/control` queue.
5. **Storage, outside the pack:** `$SAMPLER_HOME/mine/<slug>/`, holding `regions.json` (owner, rev,
   pads, prev) and the immutable WAVs. flippack `--force` on the same song renames `packs/<slug>` only,
   so `mine/` is never touched (flippack.py around line 773).
6. **Slug handed to a different song** (`choose_slug` with `slug_arg` and `--force`):
   - Inside the existing `.names.lock`, `mine/<slug>` is renamed FIRST to
     `mine/<slug>.prev-<stamp>-<pid>`, so the name can't collide. Its `regions.json` still names
     the old owner.
   - Then the audio, stems and pack are renamed, and `.source` is written last.
   - If it fails partway, the next run's `.source` check sees the old owner or a mismatch. The
     manifest's owner never matches the new song, so a stale `mine/` is refused, never shown.
   - `taken()` also counts an existing `mine/<slug>`, so an orphaned bank blocks automatic reuse of
     that slug.
7. **Decks load bank mine.**
   - A dedicated `refreshMine(d)` (not `loadPack`) fetches `/api/mine/<slug>`. It decodes the
     changed files and swaps a 16-slot array in place, with no sort and no compaction, so pad 5
     stays pad 5.
   - Revisioned file names are the cache keys, so stale audio can't replay.
   - Each deck has a refresh generation that goes up on every refresh and pack load. A result whose
     generation is stale is dropped before it touches the bank, so a slow rev 7 can't overwrite
     rev 8.
   - Loops, sync and the chosen bank are untouched, and superseded buffers are released.
   - It runs on pack load, on window focus, and on a `mine` hint for the deck's pack (both decks if
     both show it).
   - The hint is only a nudge; the fetch is the truth. A server restart that resets `/control`
     sequence numbers is covered by the focus refetch. That existing reset gap is not fixed here.
   - **Bank key:** Shift+5 = left mine, Shift+6 = right mine. Plain 5 and 6 stay sync. Both keys use
     `e.code`, so AZERTY works. This is a separate `minecode` per deck, not a fifth `bankcodes`
     entry, so the pad fallback (`12 + index`) doesn't change. Help text and the button label
     (⇧5 / ⇧6) are updated.
8. **AI picks on the wave.** flippack writes `regions` to `analysis.json`: per chop file, its stem and
   the actual clipped frames used. That includes the vocal padding, 10 ms before and 50 ms after the
   phrase (flippack.py around line 696). Synth and mix chops record their constant `barlen/4`
   positions as cut. Loops already carry `start`/`seconds`. Packs built before this show loops only
   until re-chopped. A re-chop reruns the analysis; its time and identical output are measured, not
   promised.
9. **AI parity.**
   - `vs.py chop <slug> <pad> <stem> <t0> <t1>` posts the same request as the editor, with the
     owner read from `/api/mine`.
   - `vs.py chop <slug> <pad> clear` empties a pad.
   - `vs.py mine <slug>` lists the pads.
   - `+bars` input is deferred.

## Edge cases and failure modes
| Case | Handling |
|---|---|
| Re-chop with `--force` (same song) | mine/ is outside packs/ and is untouched. test.sh proves it. |
| Slug handed to another song | mine/ is moved first, and an owner mismatch is refused. A failure partway and a retry are tested. |
| Editor open while its slug changes song | `owner` token mismatch returns 409 "this pack changed, reload". |
| Save during a chop | 409 busy, nothing written. |
| Crash or drive pull mid-save | Before the manifest rename nothing changed; after it the pad is whole. A leftover tmp or new WAV is ignored. |
| Two saves at once | The non-blocking slug lock gives one of them 409 "pack busy". The editor retries once after 300 ms. Tested: the retry lands, both pads are kept, and rev goes up by 2. |
| Not 16-bit PCM, or stems missing | View only, with the reason shown. Existing mine WAVs still play. |
| No `.source` marker | View only. mine is not listed or played: there is no owner to match. |
| Bad numbers, pad, stem, slug, traversal | 400 or 404 with the reason. Tested with encoded `..`, symlinks, HEAD, a missing X-VS and a foreign Origin. |

## Security surface
- Untrusted input: the POST body and URL paths. All of it goes through `pack_paths` and the field
  checks above.
- File names are built only from the pad number and the revision, never from user text.
- GETs serve whitelisted shapes only, with no directory listings.
- POSTs go only through `read_json()` guards. The existing browser protection holds, and no new auth
  is needed.
- Nothing from regions.json reaches HTML except through `textContent`.

## Fast loop
- `test.sh`, server: validator rejections; the exact frame count `b − a` and zeroed edges on a
  fixture PCM16 stem; 409 busy while the slug lock is held; 409 on a stale owner; an interrupted
  commit (tmp left, no rename) leaves the old manifest live, and a retry after a crash keeps the
  orphan WAV and uses a new name; overlapping saves give 409 then land on retry; clear
  keeps the old WAV; GET whitelist and traversal; a float WAV gives "can't save".
- `test.sh`, flippack: `--force` on the fixture leaves `mine/` byte-identical; slug handed to
  another song moves `mine/` (success, plus failure partway then retry); `taken()` sees `mine/`.
- `tests/sim.js`: snapping on a drifting grid and a partial last bar; `refreshMine` keeps pad slots,
  cache keys and the playing state, and refreshes finishing in reverse order keep the newest;
  Shift+5/6 pick mine, and plain 5/6 still sync.
- Headless (Playwright): the done-test end to end on Postcard. Jefe's ear is last.

## Deliberately not built (v1)
- A lyrics lane: whisper word timing. whisper is on Jefe's Mac, but adding it to the friends' setup
  is a new dependency, his call.
- Undo button, more than one user bank, renaming pads, `+bars`, rebuilding WAVs from the manifest,
  and 24-bit or float sources.
- Editing the AI's own banks in place: they stay as made, and mine is the player's.

## Review fold (KilaBz b092ba05, APPROVE-WITH-CHANGES)
All 12 findings are folded:
- 1: immutable cuts plus the manifest commit.
- 2: the chopper's slug lock, non-blocking.
- 3: the owner token, with a missing marker never matching.
- 4: mine moved first, a pid in the archive name, `taken()`.
- 5: `pack_paths`.
- 6: `read_json` plus whitelisted GETs.
- 7: PCM16 only, otherwise view-only.
- 8: `refreshMine` with revisioned keys.
- 9: the hint plus a fetch on focus.
- 10: Shift+5/6.
- 11: frames recorded, `b − a`, partial bar, beats approximate.
- 12: the pitch behavior stated, `+bars` and rebuild deferred, timing promises dropped.

Re-review KilaBz 991c04b2 (APPROVE-WITH-CHANGES): 4 more folded. The WAV is created exclusively
with a random suffix; each deck has a refresh generation; path checks are split from `owner_ok`;
overlapping saves get 409 and a retry.

Oracle could not run: Gemini quota on the Mini is exhausted, resetting about 2026-11-08.
