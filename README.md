# MyndAIX-Sampler

A two-deck, 16-pad sampler that runs in your browser. Paste a YouTube link (or drop an audio
file) and the song gets split into drums, bass, synths and vocals, cut into 8-bar loops and
16-chop banks, and put on the pads. Then you play it with your keyboard.

Your AI assistant (Claude Code, Codex, Cursor, or any agent that can run commands) does the
setup and the chopping. It follows [AGENTS.md](AGENTS.md).

## Start

You need a Mac (Linux should work too), Python 3.10 to 3.14 (the stem splitter doesn't install on
3.15 yet), ffmpeg, and an AI coding assistant.

```
git clone <this repo>
cd myndaix-sampler
```

Open your AI assistant in this folder and say: **"set up the sampler and start it."**

In the sampler, paste a YouTube link (or drop a song file on a deck) and click **Add song**. Then
tell your AI: **"chop my song."** A few minutes later the pads play it.

No AI? Do it by hand: `./setup.sh` once, `./run.sh` to start, and `./vs.py run 1` to chop request #1.

## Play

| | Left deck | Right deck |
|---|---|---|
| Pads 1-4 / 5-8 / 9-12 / 13-16 | Z X C V / A S D F / Q W E R / 1 2 3 4 | M , . / / J K L ; / U I O P / 7 8 9 0 |
| Banks: loops, synth16, mix16, vocal16 | Shift + 1 2 3 4 (or fn + F1–F4) | Shift + 7 8 9 0 (or fn + F5–F8) |
| Pitch up / down / reset | G / B / T | H / N / Y |
| Sync to the other deck | 5 | 6 |

- Pad 1 is bottom-left on each side. Space stops all loops.
- On a Mac keyboard, hold **fn** with the F-keys unless they're set to standard.
- Pitch moves speed and pitch together, a semitone a step, like an old-school sampler.
- In the loops bank, a pad starts or stops its loop on the next bar, in time with the loops already running.
- Each deck has a waveform screen: the sample you just hit, with a playhead.
- Each deck can hold a different song (menu at the top of the deck).
- **SYNC** makes that deck follow the other one: same tempo, and its loops land on the other deck's bar
  lines. A slow song locks at half time (77 BPM under 140) instead of being sped up. It keeps following
  when the other deck's pitch moves. Like a record, the key moves with the speed. Press SYNC again, or
  move that deck's own pitch, to let go.
- **×2 / ÷2** fix a tempo that was read at double or half speed.
- The mixer under the decks: a volume per deck and a crossfader (full left mutes the right deck).

## What a song turns into

| Bank | What's on the pads |
|---|---|
| loops | the best 8-bar loop of each part: full mix, drums, bass, synth/keys, vocal, guitar, piano |
| synth16 | 16 one-beat chops of the main melodic loop (usually synth/keys), in order |
| mix16 | 16 one-beat chops of the full-mix loop, in order |
| vocal16 | up to 16 vocal phrases, cut at breath gaps |

Each pack also gets a `PACK.md` (tempo, key, the scale to set on a keyboard, a map of the
song's sections) and an `ARRANGE.md` sheet to plan your own track. They're in `library/packs/<song>/`.

## Songs and copyright

This repo ships no music. Songs you download and the packs made from them stay on your computer in
`library/`, which git ignores. Downloading from YouTube may break YouTube's terms, and the songs
belong to their owners: keep them for your own practice, and clear any sample before you release
a track that uses it.

## How it works

`app/index.html` is the whole instrument (Web Audio, no build step). `serve.py` is a small local
server (127.0.0.1 only) that serves the app and the packs, and holds a queue your AI uses to chop
songs and play along. `flippack/flippack.py` does the chopping with
[demucs](https://github.com/facebookresearch/demucs) (stem splitting),
[librosa](https://librosa.org) (tempo, beats, key) and [yt-dlp](https://github.com/yt-dlp/yt-dlp)
(downloads). `./test.sh` checks the server in about 3 seconds.

Not built yet: key lock (tempo change without the key moving), recording, saving a session.
MIDI pads are mapped (notes 36-51 left, 52-67 right) but untested on real hardware.

## License

MIT, see [LICENSE](LICENSE).
