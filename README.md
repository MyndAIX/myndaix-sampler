# virtual-sampler (working name)

Jefe's own sampler, built as software first. He plays the pads; Mack can work the same instrument
at the same time (switch banks, hit pads, load packs, put messages on its screen). It plays flip
packs made by `/Volumes/Crucial Media/sampler/flippack/flippack.sh`.

v0 prototype, built 2026-11-01. Not reviewed. Run the full /feature pipeline before it grows into a
product.

## Use
```
./run.sh                 # starts the server on 127.0.0.1:8765 and opens the browser
./vs.py state            # Mack: what is loaded, which bank, recent pad hits (who hit them)
./vs.py bank vocal16     # Mack: switch banks; also: pad N, load <slug>, stop, say "text"
./test.sh                # ~2 s server contract check
```
Keys: Z X C V = pads 1-4 (bottom row), A S D F = 5-8, Q W E R = 9-12, 1 2 3 4 = 13-16.
`[` `]` change bank, Space stops loops. In the loops bank a pad starts or stops its loop on the next
bar, in phase with the loops already running.

## How it fits together
- `app/index.html`: the whole instrument (Web Audio). It reads the pack's `analysis.json`
  (`files`, `bpm`, `barlen`, `scale`), decodes every wav up front, and polls `/control` every 150 ms.
- `serve.py`: stdlib HTTP server, localhost only. Serves the app, the packs read-only, a command
  queue (`/control`) and the app's reported state (`/state`). POSTs need the `X-VS: 1` header so other
  web pages can't drive it.
- `vs.py`: Mack's command line for the queue and state.

## Not built yet
- MK2 pads: the app maps MIDI note 36 to pad 1, but that is untested. Maschine owns the MK2 in its
  own mode; MIDI mode needs Controller Editor, whose Connect button failed on 2026-08-30.
- Recording or sequencing, per-pad volume/pitch, effects, saving a session.
