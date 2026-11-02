#!/usr/bin/env python3
"""Flip pack: YouTube link (or a wav, or an already-split slug) -> stems, 8-bar loops,
chop sets, tempo/key, a section map of the original, and a blank arrangement sheet.

Usage:  flippack.py SOURCE [--slug NAME] [--force]
  SOURCE = https://... (ripped with yt-dlp) | path/to/song.wav | existing slug under stems/
Output: $SAMPLER_HOME/packs/<slug>/  (stems stay in $SAMPLER_HOME/stems/htdemucs_6s/<slug>/)
SAMPLER_HOME defaults to library/ in this repo.

Section names (intro/verse/hook/break) are GUESSES from which stems play and what repeats;
bar numbers, times, tempo and the cut points are measured.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

DEFAULT_HOME = Path(__file__).resolve().parent.parent / "library"
ROOT = Path(os.environ.get("SAMPLER_HOME") or DEFAULT_HOME)
INBOX, STEMS_DIR, PACKS = ROOT / "inbox", ROOT / "stems/htdemucs_6s", ROOT / "packs"
STEMS = ["drums", "bass", "other", "vocals", "guitar", "piano"]
LABEL = {"drums": "drums", "bass": "bass", "other": "synth/keys", "vocals": "vocal",
         "guitar": "guitar", "piano": "piano"}
SR = 22050                      # analysis rate; cuts are made from the full-rate stem files
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def slugify(title):
    s = title.lower()
    s = re.sub(r"\((official|lyric|lyrics|audio|video|hd|hq|remaster(ed)?)[^)]*\)", "", s)
    s = re.sub(r"\[(official|lyric|lyrics|audio|video|hd|hq|remaster(ed)?)[^\]]*\]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60].strip("-")
    return s or "untitled"


# ---------- 1. source -> wav -> stems ----------

def check_home():
    # A SAMPLER_HOME that is missing is usually an unplugged drive. Creating it would write the
    # library onto the internal disk under /Volumes, so only the default library is ever created.
    if ROOT != DEFAULT_HOME and not ROOT.is_dir():
        sys.exit(f"SAMPLER_HOME {ROOT} does not exist (is the drive plugged in?)")
    for d in (INBOX, STEMS_DIR, PACKS):
        d.mkdir(parents=True, exist_ok=True)


def ytdlp_cmd():
    # setup.sh installs yt-dlp into the venv; a system yt-dlp (e.g. Homebrew) also works
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    if not exe:
        sys.exit("yt-dlp not found: run ./setup.sh (or brew install yt-dlp)")
    return [exe]


def rip(url):
    """Download to a name keyed by the video's id (two videos with the same title can't collide).
    Returns (wav path, title, identity)."""
    # argv list, no shell: the URL is never interpreted by a shell
    r = subprocess.run(ytdlp_cmd() + ["--no-playlist", "-x", "--audio-format", "wav",
                        "-o", str(INBOX / ".dl-%(extractor_key)s-%(id)s.%(ext)s"),
                        "--print", "after_move:%(extractor_key)s:%(id)s",
                        "--print", "after_move:title",
                        "--print", "after_move:filepath", "--no-simulate", url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"yt-dlp failed:\n{r.stderr[-800:]}")
    out = r.stdout.strip().splitlines()
    if len(out) < 3:
        sys.exit(f"yt-dlp printed too little: {r.stdout[-300:]!r}")
    ident, title, path = out[-3], out[-2], Path(out[-1])
    if not path.is_file() or path.parent.resolve() != INBOX.resolve():
        sys.exit(f"yt-dlp reported {path} but it is not in the inbox")
    return path, title, ident


def file_identity(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def pick_slug(base, ident):
    """The first of base, base-2, base-3 ... that is free or already belongs to this same song.
    inbox/<slug>.source records which song a slug belongs to, so a different song whose title or
    file name makes the same slug gets its own name instead of reusing the first song's audio,
    stems and pack. A slug with files but no .source (made before this existed, or by hand) is
    never reused."""
    for k in range(1, 1000):
        slug = base if k == 1 else f"{base[:75]}-{k}"
        marker = INBOX / f"{slug}.source"
        if marker.exists():
            if marker.read_text().strip() == ident:
                return slug
            continue
        if any(os.path.lexists(x) for x in (INBOX / f"{slug}.wav", STEMS_DIR / slug, PACKS / slug)):
            continue
        try:
            # O_EXCL: two runs at once can't both claim the same free slug
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            if marker.read_text().strip() == ident:
                return slug
            continue
        with os.fdopen(fd, "w") as f:
            f.write(ident + "\n")
        return slug
    sys.exit(f"no free name for {base}")


def check_audio(path):
    # before demucs (minutes of work): refuse audio that can't make an 8-bar loop
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as e:
        sys.exit(f"could not read {path.name} as audio: {e}")
    secs = len(data) / sr if sr else 0
    if secs < 20:
        sys.exit(f"{path.name} is {secs:.1f} s long; a song needs at least 20 s to make loops")
    if not np.isfinite(data).all() or float(np.max(np.abs(data))) < 1e-3:
        sys.exit(f"{path.name} is silent")


def get_stems(source, slug_arg):
    if re.match(r"^https?://", source):
        log("ripping audio")
        wav, title, ident = rip(source)
    elif Path(source).is_file():
        wav = Path(source)
        title, ident = wav.stem, None
    elif SLUG_RE.match(source) and (STEMS_DIR / source).is_dir():
        return source, STEMS_DIR / source, None
    else:
        sys.exit(f"SOURCE is not a URL, a file, or a slug with stems: {source}")
    if slug_arg:
        slug = slug_arg         # an explicit --slug is used as given (with --force to rebuild)
    else:
        slug = pick_slug(slugify(title), ident or file_identity(wav))
    if not SLUG_RE.match(slug):
        sys.exit(f"bad slug {slug!r}: use lowercase letters, digits and dashes")
    target = INBOX / f"{slug}.wav"
    if wav.resolve() != target.resolve():
        if target.exists():
            log(f"inbox already has {target.name}; using it")
            if wav.parent.resolve() == INBOX.resolve() and wav.name.startswith(".dl-"):
                wav.unlink()    # the fresh download of the same song is a duplicate
        elif wav.suffix.lower() != ".wav":
            # mp3/m4a/flac/aiff: decode to wav once so every later step reads the same file
            log(f"converting {wav.name} to wav")
            # into a temp name first: a failed run must not leave a half file a retry would trust
            tmp = target.with_name(f".{target.stem}.converting.wav")
            r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(wav),
                                "-ac", "2", "-ar", "44100", str(tmp)])
            if r.returncode != 0:
                tmp.unlink(missing_ok=True)
                sys.exit(f"ffmpeg could not read {wav.name}")
            tmp.replace(target)
        else:
            shutil.copy2(wav, target) if wav.parent.resolve() != INBOX.resolve() else wav.rename(target)
    check_audio(target)
    sdir = STEMS_DIR / slug
    if all((sdir / f"{s}.wav").exists() for s in STEMS):
        log(f"stems already exist for {slug}; skipping separation")
    else:
        log("separating stems with demucs htdemucs_6s (a few minutes)")
        # demucs from the same Python that runs this script, so any venv works
        r = subprocess.run([sys.executable, "-m", "demucs", "-n", "htdemucs_6s",
                            "-o", str(ROOT / "stems"), str(target)])
        if r.returncode != 0 or not all((sdir / f"{s}.wav").exists() for s in STEMS):
            sys.exit("demucs failed; see output above")
    return slug, sdir, target


# ---------- 2. analysis ----------

def beat_grid(drums, mix):
    """Return (beats, bpm, steady, resid_ms). Uses the drum stem; falls back to the mix."""
    src = drums if np.sqrt(np.mean(drums ** 2)) > 0.01 else mix
    tempo, _ = librosa.beat.beat_track(y=src, sr=SR)
    t0 = float(np.atleast_1d(tempo)[0])
    if not np.isfinite(t0) or t0 <= 0:
        # a zero tempo would make the doubling loop below run forever
        sys.exit("no beat found in this song")
    while t0 < 75:
        t0 *= 2
    while t0 >= 150:
        t0 /= 2
    _, beats = librosa.beat.beat_track(y=src, sr=SR, start_bpm=t0, units="time")
    if len(beats) < 16:
        sys.exit(f"only {len(beats)} beats found; need a steady beat for at least 4 bars")
    k = np.arange(len(beats))
    slope, icpt = np.polyfit(k, beats, 1)
    resid = np.abs(beats - (icpt + slope * k))
    med = float(np.median(resid) * 1000)
    steady = med < 15
    # steady tempo: the fitted line is more accurate than per-beat tracking jitter
    grid = icpt + slope * k if steady else beats
    return grid, 60 / slope, steady, med, src is drums


def band_env(y):
    D = np.abs(librosa.stft(y, n_fft=2048, hop_length=256))
    f = librosa.fft_frequencies(sr=SR, n_fft=2048)
    t = librosa.frames_to_time(np.arange(D.shape[1]), sr=SR, hop_length=256)
    return t, D[f < 120].sum(0), D[(f > 1500) & (f < 5000)].sum(0)


def downbeat_phase(beats, drums, y):
    """Kick on 1+3 / snare on 2+4 picks the pair; section changes landing on beat 1 pick
    which of the pair. Same method as the hand-checked Smalltown Boy analysis."""
    t, low, mid = band_env(drums)
    at = lambda env: np.array([env[np.argmin(np.abs(t - x))] for x in beats])
    L, M = at(low), at(mid)
    # four-on-the-floor songs have a kick on every beat, so the snare side (mid band on 2+4)
    # carries most of the signal here
    pair = max((0, 1), key=lambda q: (L[q::2].mean() / L.mean()) - (M[q::2].mean() / M.mean()))
    # Phrase novelty: harmony/timbre change between the 4 beats before and the 4 beats after
    # each beat. Plain energy change picked beat 3 on Smalltown Boy (2 beats early); this
    # method picked the hand-verified downbeat.
    tot = np.zeros(len(beats) - 1)
    for s in ("other", "vocals", "bass", "drums"):
        if s == "drums":
            Cf = librosa.feature.mfcc(y=y[s], sr=SR, hop_length=512)
        else:
            Cf = librosa.feature.chroma_cqt(y=y[s], sr=SR, hop_length=512)
        t2 = librosa.frames_to_time(np.arange(Cf.shape[1]), sr=SR, hop_length=512)
        B = np.array([Cf[:, (t2 >= a) & (t2 < b)].mean(1) if np.any((t2 >= a) & (t2 < b))
                      else np.zeros(Cf.shape[0]) for a, b in zip(beats[:-1], beats[1:])])
        B = (B - B.mean(0)) / (B.std(0) + 1e-6)
        nov = np.zeros(len(B))
        for i in range(4, len(B) - 4):
            nov[i] = np.linalg.norm(B[i:i + 4].mean(0) - B[i - 4:i].mean(0))
        tot += nov / (nov.max() + 1e-9)
    # tot[j] = change entering beat j
    score = {p: float(tot[p::4].mean()) for p in (pair, pair + 2)}
    return max(score, key=lambda k: score[k]), score


def extend(beats, dur):
    step = float(np.median(np.diff(beats)))
    pre = []
    t = beats[0] - step
    while t >= 0:
        pre.append(t)
        t -= step
    post = []
    t = beats[-1] + step
    while t + step <= dur:
        post.append(t)
        t += step
    return np.concatenate([pre[::-1], beats, post]), len(pre)


def key_of(y):
    harm = sum(y[s] for s in ("bass", "other", "guitar", "piano"))
    c = librosa.feature.chroma_cqt(y=harm, sr=SR, hop_length=2048).mean(1)
    maj = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    mnr = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    res = []
    for i in range(12):
        res.append((np.corrcoef(c, np.roll(maj, i))[0, 1], f"{NOTES[i]} major"))
        res.append((np.corrcoef(c, np.roll(mnr, i))[0, 1], f"{NOTES[i]} minor"))
    res.sort(reverse=True)
    return res[0][1], float(res[0][0]), res[1][1], float(res[1][0])


# Maschine Keyboard-mode scales (Main bank), smallest first so a pentatonic wins when it fits
MASCHINE_SCALES = [("Min Pent", (0, 3, 5, 7, 10)), ("Maj Pent", (0, 2, 4, 7, 9)),
                   ("Minor", (0, 2, 3, 5, 7, 8, 10)), ("Major", (0, 2, 4, 5, 7, 9, 11))]


def scale_of(y):
    """Which notes the song actually plays (pitch tracking on bass, synth/keys and vocal), and the
    smallest Maschine scale holding all of them. Chroma-based key guesses can't tell relative keys
    apart (Smalltown Boy read A# major; it plays only C D# F G A#, which is C Min Pent)."""
    hist, peak, bass_h = np.zeros(12), np.zeros(12), np.zeros(12)
    for s, lo, hi, fl in (("bass", 30, 300, 4096), ("other", 60, 1000, 2048), ("vocals", 60, 1000, 2048)):
        if np.sqrt(np.mean(y[s] ** 2)) < 0.005:
            continue
        f0, vf, vp = librosa.pyin(y[s], fmin=lo, fmax=hi, sr=SR, frame_length=fl, hop_length=1024)
        ok = vf & (vp > 0.6) & np.isfinite(f0)
        if ok.sum() < 50:
            continue
        h = np.bincount(np.round(librosa.hz_to_midi(f0[ok])).astype(int) % 12, minlength=12) / ok.sum()
        hist += h
        peak = np.maximum(peak, h)
        if s == "bass":
            bass_h = h
    if hist.sum() == 0:
        return None
    hist /= hist.sum()
    # a note counts if any ONE part leans on it: averaging hid the vocal's G on Smalltown (11% of the
    # vocal, 0% bass/synth) and picked F Min Pent, which has an Ab the song never plays
    # 8%: vocal slides and vibrato leave ~5% on the neighbouring semitone (Smalltown: F# 5%, real G 11%)
    used = {i for i in range(12) if peak[i] >= 0.08}
    best = None
    for rank, (name, iv) in enumerate(MASCHINE_SCALES):
        for r in range(12):
            if used <= {(r + i) % 12 for i in iv}:
                cand = (rank, -bass_h[r], name, r)    # same notes, two roots: the one the bass sits on more
                best = min(best, cand) if best else cand
    notes = [NOTES[i] for i in sorted(used, key=lambda i: -hist[i])]
    if best is None:
        return dict(notes=notes, scale=None, root=None)
    return dict(notes=notes, scale=best[2], root=NOTES[best[3]])


def bar_levels(y, bars):
    out = {}
    for s in STEMS:
        r = np.array([np.sqrt(np.mean(y[s][int(a * SR):int(b * SR)] ** 2))
                      for a, b in zip(bars[:-1], bars[1:])])
        out[s] = r
    return out


def sections(lv, bars, chroma_bars, anchor):
    """4-bar blocks aligned to the first drum downbeat, classed by drums (off/light/full) and
    vocal (off/lead/big), smoothed so 4-bar blips fold into their neighbours, then letters
    for material that repeats. Bass vs synth/keys is NOT used to split sections: the
    separator moves synth bass between those two stems from section to section."""
    nb = len(bars) - 1
    ref = {s: np.percentile(lv[s], 95) + 1e-9 for s in STEMS}
    loud = max(ref.values())
    # stems more than 10 dB under the loudest are bleed or near-empty; leave them out of the map
    main = [s for s in STEMS if 20 * np.log10(ref[s] / loud) > -10]
    starts = list(range(anchor % 4, nb, 4))
    if starts[0] > 0:
        starts = [0] + starts
    spans = [(a, starts[i + 1] if i + 1 < len(starts) else nb) for i, a in enumerate(starts)]
    norm = lambda s, a, b: float(lv[s][a:b].mean() / ref[s])
    cls = lambda v, lo, hi: 0 if v < lo else (1 if v < hi else 2)
    dc = [cls(norm("drums", a, b), 0.15, 0.6) for a, b in spans]
    vc = [cls(norm("vocals", a, b), 0.15, 0.75) for a, b in spans]

    def smooth(c):
        out = c[:]
        for i in range(1, len(c) - 1):
            if c[i - 1] == c[i + 1] != c[i]:
                out[i] = c[i - 1]
        return out
    dc, vc = smooth(dc), smooth(vc)
    segs = []
    for (a, b), d, v in zip(spans, dc, vc):
        if segs and segs[-1]["d"] == d and segs[-1]["v"] == v:
            segs[-1]["b"] = b
        else:
            segs.append(dict(a=a, b=b, d=d, v=v))
    # leading drumless blocks are one intro; a short quiet tail is the outro
    # everything before the drums' first downbeat is intro (a pre-drop fill reads as "light drums")
    while len(segs) > 1 and segs[0]["d"] == 0 and (segs[1]["d"] == 0 or segs[1]["b"] <= anchor):
        segs[0]["b"] = segs[1]["b"]      # keep segs[0]'s drumless class so it guesses "intro"
        segs.pop(1)
    for s in segs:
        a, b = s["a"], s["b"]
        ch = chroma_bars[a:b].mean(0)
        s["ch"] = ch / (np.linalg.norm(ch) + 1e-9)
        s["en"] = float(sum(lv[x][a:b].mean() / ref[x] for x in main))
        s["act"] = tuple(x for x in main if np.mean(lv[x][a:b] > 0.2 * ref[x]) >= 0.5)
    protos = []
    for s in segs:
        for li, (d, v, ch) in enumerate(protos):
            if (d, v) == (s["d"], s["v"]) and float(ch @ s["ch"]) > 0.9:
                s["letter"] = chr(65 + li)
                break
        else:
            protos.append((s["d"], s["v"], s["ch"]))
            s["letter"] = chr(65 + len(protos) - 1)
    med = float(np.median([s["en"] for s in segs]))
    for i, s in enumerate(segs):
        if i == 0 and s["d"] == 0:
            g = "intro"
        elif i == len(segs) - 1 and s["en"] < med:
            g = "outro"
        elif s["d"] == 0:
            g = "breakdown?"
        elif s["v"] == 2:
            g = "hook?"
        elif s["v"] == 1:
            g = "verse?" + (" (light drums)" if s["d"] == 1 else "")
        else:
            g = "instrumental?"
        s["guess"] = g
    return segs


# ---------- 3. cutting ----------

def fade(x, sr, ms):
    f = max(1, int(ms / 1000 * sr))
    x[:f] *= np.linspace(0, 1, f)[:, None]
    x[-f:] *= np.linspace(1, 0, f)[:, None]
    return x


def cut(data, sr, t0, t1, ms=2):
    a, b = int(round(t0 * sr)), int(round(t1 * sr))
    return fade(data[a:b].copy(), sr, ms)


def best_window(target, lv, nb, anchor, w, L=8):
    norm = {s: lv[s] / (np.percentile(lv[s], 95) + 1e-9) for s in lv}
    others = [s for s in norm if s != target and np.percentile(lv[s], 95) > 0.01]
    best = None
    for i in range(anchor % 4, nb - L + 1, 4):
        sc = norm[target][i:i + L].min() - w * (np.mean([norm[o][i:i + L].mean() for o in others])
                                                 if others else 0)
        if best is None or sc > best[0]:
            best = (sc, i)
    return best[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--slug")
    ap.add_argument("--force", action="store_true", help="keep the old pack as <slug>.prev-<time> and rebuild")
    args = ap.parse_args()
    if args.slug and not SLUG_RE.match(args.slug):
        sys.exit("--slug: lowercase letters, digits and dashes only")
    check_home()

    slug, sdir, mixwav = get_stems(args.source, args.slug)
    if mixwav is None:
        cand = INBOX / f"{slug}.wav"
        mixwav = cand if cand.exists() else None
    out = PACKS / slug
    if out.exists() and not args.force:
        sys.exit(f"{out} exists; pass --force to rebuild (the old one is kept, renamed)")

    log("loading stems")
    y = {s: librosa.load(str(sdir / f"{s}.wav"), sr=SR, mono=True)[0] for s in STEMS}
    mix = librosa.load(str(mixwav), sr=SR, mono=True)[0] if mixwav else sum(y.values())
    dur = len(y["drums"]) / SR

    log("beat grid + downbeat")
    beats, bpm, steady, resid, from_drums = beat_grid(y["drums"], mix)
    phase, pscore = downbeat_phase(beats, y["drums"], y)
    first_drum_bar = float(beats[phase])
    allb, npre = extend(beats, dur)
    p = (phase + npre) % 4
    bars = allb[p::4]
    if dur - bars[-1] > 0.25 * (bars[1] - bars[0]):
        bars = np.append(bars, dur)
    anchor = int(np.argmin(np.abs(bars - first_drum_bar)))
    barlen = float(np.median(np.diff(bars)))
    nb = len(bars) - 1
    if nb < 12:
        # the loop finder needs room for an 8-bar window starting on a 4-bar boundary
        sys.exit(f"only {nb} bars; a song needs at least 12 bars to make loops")

    log("key + levels + sections")
    key, kc, key2, kc2 = key_of(y)
    log("pitch tracking for the scale (about a minute)")
    sc = scale_of(y)
    lv = bar_levels(y, bars)
    harm = sum(y[s] for s in ("bass", "other", "guitar", "piano"))
    C = librosa.feature.chroma_cqt(y=harm, sr=SR, hop_length=1024)
    ct = librosa.frames_to_time(np.arange(C.shape[1]), sr=SR, hop_length=1024)
    chroma_bars = np.array([C[:, (ct >= a) & (ct < b)].mean(1) if np.any((ct >= a) & (ct < b))
                            else np.zeros(12) for a, b in zip(bars[:-1], bars[1:])])
    segs = sections(lv, bars, chroma_bars, anchor)

    tmp = PACKS / f".{slug}.tmp"
    if tmp.exists():
        tmp.rename(PACKS / f".{slug}.tmp-stale-{int(time.time())}")
    for d in ("loops", "chops/synth16", "chops/vocal16", "chops/mix16"):
        (tmp / d).mkdir(parents=True, exist_ok=True)

    log("cutting loops")
    full = {s: sf.read(str(sdir / f"{s}.wav"), always_2d=True) for s in STEMS}
    fsr = full["drums"][1]
    subtype = sf.info(str(sdir / "drums.wav")).subtype
    weights = {"drums": 1.0, "bass": 0.2, "other": 0.5, "vocals": 0.2, "guitar": 0.5, "piano": 0.5}
    p95 = {s: float(np.percentile(lv[s], 95)) for s in STEMS}
    loudest = max(p95.values())
    loops, n = {}, 0
    for s in STEMS:
        if p95[s] < 0.01:      # stem is basically empty (about -40 dBFS)
            continue
        i = best_window(s, lv, nb, anchor, weights[s])
        n += 1
        name = f"{n} {LABEL[s].replace('/', '-')} 8bar.wav"
        sf.write(str(tmp / "loops" / name), cut(full[s][0], fsr, bars[i], bars[i + 8]), fsr, subtype=subtype)
        rel = 20 * np.log10(p95[s] / loudest)
        # same 10 dB line sections() uses for "main" stems; quieter stems are usually demucs bleed
        loops[s] = dict(file=name, bar=i + 1, start=round(float(bars[i]), 3),
                        seconds=round(float(bars[i + 8] - bars[i]), 3), rel_db=round(float(rel), 1),
                        faint=bool(rel <= -10))
    # the classic sample: 8 bars of the full mix from the loudest hook-guess section
    hook_segs = [s for s in segs if s["guess"] == "hook?"] or sorted(segs, key=lambda s: -s["en"])[:1]
    hs = hook_segs[0]
    # start on the 8-bar phrase line (counted from the first drum bar) inside the hook, not the
    # 4-bar block where the map's hook begins: Smalltown's map says bar 77, the hook lands on 81
    lines8 = [x for x in range(anchor % 8, nb, 8) if hs["a"] <= x and x + 8 <= hs["b"]]
    mi = lines8[0] if lines8 else (hs["a"] if hs["a"] + 8 <= nb else max(0, nb - 8))
    md = msr = None
    if mixwav:
        md, msr = sf.read(str(mixwav), always_2d=True)
        sf.write(str(tmp / "loops" / "0 full mix 8bar.wav"), cut(md, msr, bars[mi], bars[mi + 8]), msr,
                 subtype=sf.info(str(mixwav)).subtype)
        loops["mix"] = dict(file="0 full mix 8bar.wav", bar=mi + 1, start=round(float(bars[mi]), 3),
                            seconds=round(float(bars[mi + 8] - bars[mi]), 3))

    log("cutting chops")
    beat = barlen / 4
    chops = {}
    mel = "other" if "other" in loops else next((s for s in ("guitar", "piano") if s in loops), None)
    if mel:
        t0 = bars[loops[mel]["bar"] - 1]
        for k in range(16):
            sf.write(str(tmp / f"chops/synth16/{LABEL[mel].split('/')[0]} {k + 1:02d}.wav"),
                     cut(full[mel][0], fsr, t0 + k * beat, t0 + (k + 1) * beat, 3), fsr, subtype=subtype)
        chops["synth16"] = f"16 one-beat hits of the {LABEL[mel]} loop, in order (bar {loops[mel]['bar']})"
    if md is not None:
        t0 = bars[mi]
        for k in range(16):
            sf.write(str(tmp / f"chops/mix16/mix {k + 1:02d}.wav"),
                     cut(md, msr, t0 + k * beat, t0 + (k + 1) * beat, 3), msr,
                     subtype=sf.info(str(mixwav)).subtype)
        chops["mix16"] = f"16 one-beat hits of the full mix, in order (bar {mi + 1})"
    # vocal phrases: split at gaps, keep the 16 loudest 0.3-3 s pieces, in song order
    iv = librosa.effects.split(y["vocals"], top_db=30, frame_length=2048, hop_length=512)
    cand = []
    for a, b in iv:
        d = (b - a) / SR
        if 0.3 <= d <= 3.0:
            cand.append((float(np.sqrt(np.mean(y["vocals"][a:b] ** 2))), a / SR, b / SR))
    cand = sorted(sorted(cand, reverse=True)[:16], key=lambda c: c[1])
    vrows = []
    for k, (_, a, b) in enumerate(cand):
        sf.write(str(tmp / f"chops/vocal16/vocal {k + 1:02d}.wav"),
                 cut(full["vocals"][0], fsr, max(0, a - 0.01), b + 0.05, 5), fsr, subtype=subtype)
        bar_no = int(np.searchsorted(bars, a, side="right"))
        vrows.append((k + 1, round(a, 2), round(b - a, 2), bar_no))
    if vrows:
        chops["vocal16"] = f"{len(vrows)} vocal phrases cut at breath gaps, in song order"

    log("writing PACK.md + ARRANGE.md")
    fmt = lambda t: f"{int(t // 60)}:{t % 60:04.1f}"
    lines = [f"# {slug}: flip pack", "",
             f"- **Tempo:** {bpm:.2f} BPM ({'steady' if steady else 'DRIFTS: loops cut on tracked beats; set Maschine to this average'})"
             f"; grid from the {'drum stem' if from_drums else 'full mix'}, median beat error {resid:.0f} ms",
             *([f"- **Maschine scale:** root **{sc['root']}**, type **{sc['scale']}** (Main bank). Every pad stays in key.",
                f"- **Notes it plays:** {', '.join(sc['notes'])} (most used first, pitch-tracked)"]
               if sc and sc["scale"] else
               [f"- **Notes it plays:** {', '.join(sc['notes'])}; no simple Maschine scale holds them all"] if sc else []),
             f"- **Key (rough guess):** {key}, runner-up {key2}. Trust the notes line above over this.",
             f"- **Bar length:** {barlen:.3f} s. Drums first land on bar {anchor + 1} ({fmt(first_drum_bar)}).",
             f"- **Length:** {fmt(dur)}, {nb} bars",
             f"- **Stems:** `{sdir}`", ""]
    if anchor > 0:
        lines.append(f"Bars 1 to {anchor} come before the drums; their bar lines are extended from the drum grid, so treat them as approximate.")
        lines.append("")
    lines += ["## Section map of the original", "",
              "Same letter = same material repeating. Names ending in ? are guesses from what plays.", "",
              "| # | bars | starts | length | playing | letter | guess |", "|---|---|---|---|---|---|---|"]
    for i, s in enumerate(segs):
        playing = ", ".join(LABEL[x] for x in s["act"]) or "(quiet)"
        lines.append(f"| {i + 1} | {s['a'] + 1}-{s['b']} | {fmt(bars[s['a']])} | {s['b'] - s['a']} bars | "
                     f"{playing} | {s['letter']} | {s['guess']} |")
    lines += ["", "## Loops (8 bars, cut on the bar line)", "", "| file | from bar | starts | note |", "|---|---|---|---|"]
    for s, v in loops.items():
        note = (f"faint ({v['rel_db']:.0f} dB under the loudest part): probably bleed, check by ear"
                if v.get("faint") else "")
        lines.append(f"| `loops/{v['file']}` | {v['bar']} | {fmt(v['start'])} | {note} |")
    lines += ["", "## Chop sets", ""] + [f"- `chops/{k}/`: {v}" for k, v in chops.items()]
    if vrows:
        lines += ["", "| vocal chop | at | length | bar |", "|---|---|---|---|"]
        lines += [f"| {k:02d} | {fmt(a)} | {d} s | {bn} |" for k, a, d, bn in vrows]
    lines += ["", "## Into Maschine", "",
              f"- Set the tempo to **{bpm:.2f}**.",
              *([f"- For melodies or bass you play yourself: Keyboard mode, root {sc['root']}, scale {sc['scale']}."]
                if sc and sc["scale"] else []),
              "- Loops go on one group, one loop per pad. Use a single 8-bar pattern with a note on step 1 for each pad.",
              "  Turn on Audio Mute for each loop pad (Channel > Output) so MUTE cuts the sound and brings it back in time.",
              "- Chop sets go one per group, pads 1-16 in file order. Pad 1 is bottom-left.", ""]
    (tmp / "PACK.md").write_text("\n".join(lines))

    plan = [("Intro", 8, "mix chops, filtered"), ("Verse 1", 16, "drums + bass loops"),
            ("Hook", 8, "synth loop + vocal chops"), ("Verse 2", 16, ""), ("Hook", 8, ""),
            ("Break", 8, "drop the drums; chops only"), ("Hook", 8, "everything"), ("Outro", 8, "")]
    rows, t = [], 0.0
    bar_no = 1
    for name, length, idea in plan:
        rows.append(f"| {name} | {bar_no}-{bar_no + length - 1} | {fmt(t)} | {length} | {idea} | |")
        bar_no += length
        t += length * barlen
    arr = [f"# {slug}: arrangement sheet", "",
           f"Tempo {bpm:.2f}. One bar = {barlen:.2f} s, 8 bars = {8 * barlen:.1f} s. "
           "Edit the plan, the bar counts and the ideas. Each row becomes a Scene in Maschine "
           "(Ideas view), then a Section in Song view.", "",
           "| section | bars | starts | length | idea (starter) | your notes |", "|---|---|---|---|---|---|"] + rows + \
          ["", f"Total: {bar_no - 1} bars = {fmt(t)}.", "",
           "Starter ideas only. The section map in PACK.md shows how the original song is built."]
    (tmp / "ARRANGE.md").write_text("\n".join(arr))
    meta = dict(slug=slug, bpm=round(bpm, 3), steady=steady, resid_ms=round(resid, 1), key=key,
                key_fit=round(kc, 3), scale=sc, barlen=round(barlen, 5), first_drum_bar_s=round(first_drum_bar, 4),
                first_drum_bar=anchor + 1, nbars=nb, bars_s=[round(float(b), 4) for b in bars],
                downbeat_score={int(k): round(float(v), 4) for k, v in pscore.items()},
                loops=loops, chops=chops,
                files=sorted(str(f.relative_to(tmp)) for f in tmp.rglob("*.wav")),
                sections=[dict(bars=[s["a"] + 1, s["b"]], playing=list(s["act"]), letter=s["letter"],
                               guess=s["guess"]) for s in segs])
    (tmp / "analysis.json").write_text(json.dumps(meta, indent=1))

    if out.exists():
        out.rename(PACKS / f"{slug}.prev-{time.strftime('%Y%m%d-%H%M%S')}")
    tmp.rename(out)
    log(f"done: {out}")
    print(json.dumps(dict(pack=str(out), bpm=meta["bpm"], key=key, first_drum_bar_s=meta["first_drum_bar_s"],
                          loops={k: v["start"] for k, v in loops.items()},
                          sections=len(segs))))


if __name__ == "__main__":
    main()
