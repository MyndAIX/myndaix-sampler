# virtual-sampler (working name)

Jefe's own sampler, built as software first. He plays the pads; Mack can work the same instrument
at the same time (switch banks, hit pads, change pitch, load packs, put messages on its screen). It
plays flip packs made by `/Volumes/Crucial Media/sampler/flippack/flippack.sh`.

Two decks, left and right, 16 pads each. Each deck loads its own pack and bank and has its own
pitch, and a waveform screen showing the last sample hit: the played part turns orange behind a
playhead, and a loop shows which of its bars is playing.

v0 prototype, built 2026-11-01. Not reviewed. Run the full /feature pipeline before it grows into a
product.

## Use
```
./run.sh                   # starts the server on 127.0.0.1:8765 and opens the browser
./vs.py state              # Mack: what each deck has loaded, bank, pitch, recent pad hits (who hit them)
./vs.py bank R vocal16     # Mack: DECK is L or R; also: pad DECK N, pitch DECK -12..12, load DECK <slug>
./vs.py stop               # stop all loops; also: say "text"
./test.sh                  # ~2 s server contract check
```

Keys (pad 1 is bottom-left on each side):

| | Left deck | Right deck |
|---|---|---|
| Pads 1-4 / 5-8 / 9-12 / 13-16 | Z X C V / A S D F / Q W E R / 1 2 3 4 | M , . / / J K L ; / U I O P / 7 8 9 0 |
| Banks (loops, synth16, mix16, vocal16) | F1 F2 F3 F4 | F5 F6 F7 F8 |
| Pitch up / down / reset | G / B / T | H / N / Y |

Space stops all loops. On a Mac keyboard hold fn with the F-keys unless they are set to standard.
Pitch moves speed and pitch together, one semitone a step (classic sampler behavior). In the loops
bank a pad starts or stops its loop on the next bar, in phase with the loops already running on that
deck.

## How it fits together
- `app/index.html`: the whole instrument (Web Audio). It reads each pack's `analysis.json`
  (`files`, `bpm`, `barlen`, `scale`), decodes every wav up front, and polls `/control` every 150 ms.
- `serve.py`: stdlib HTTP server, localhost only. Serves the app, the packs read-only, a command
  queue (`/control`) and the app's reported state (`/state`). POSTs need the `X-VS: 1` header so other
  web pages can't drive it.
- `vs.py`: Mack's command line for the queue and state.

## Not built yet
- MK2 pads: the app maps MIDI notes 36-51 to the left deck and 52-67 to the right, but that is
  untested. Maschine owns the MK2 in its own mode; MIDI mode needs Controller Editor, whose Connect
  button failed on 2026-08-30.
- A single master pitch (today pitch is per deck), recording or sequencing, per-pad volume,
  effects, saving a session.
