#!/bin/bash
# Server contract check (~3 s), no songs needed: builds a one-chop fake pack in a temp library,
# then checks files, path safety, the command queue, state, song requests and uploads.
# If setup.sh has been run, it also checks the chopper: bad audio, song names and clashes,
# the dropped-file retry, --slug, and concurrent runs (~15 s).
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT=8766
LIB="$(mktemp -d)"
pid=""
trap 'if [ -n "$pid" ]; then kill "$pid" 2>/dev/null; fi; rm -rf "$LIB"' EXIT INT TERM

# a missing SAMPLER_HOME (an unplugged drive) must stop the server, not get created
if SAMPLER_HOME="$LIB/not-there" python3 -I "$HERE/serve.py" "$PORT" > /dev/null 2>&1; then
  echo "FAIL  server started on a missing SAMPLER_HOME"; exit 1
fi
[ -e "$LIB/not-there" ] && { echo "FAIL  missing SAMPLER_HOME was created"; exit 1; }
echo "PASS  missing SAMPLER_HOME refused, not created"

python3 -I - "$LIB" <<'PY'
import json, struct, sys, wave
from pathlib import Path
pack = Path(sys.argv[1]) / "packs" / "test-pack"
(pack / "chops" / "synth16").mkdir(parents=True)
with wave.open(str(pack / "chops" / "synth16" / "synth 01.wav"), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
    w.writeframes(struct.pack("<2205h", *([1000, -1000] * 1102 + [0])))
(pack / "analysis.json").write_text(json.dumps({"bpm": 120, "barlen": 2.0, "files": ["chops/synth16/synth 01.wav"]}))
# a pack moved aside by a --force re-chop: on disk, never in the deck menu
old = pack.parent / "test-pack.prev-20260101-000000"
old.mkdir(); (old / "analysis.json").write_text((pack / "analysis.json").read_text())
PY

SAMPLER_HOME="$LIB" python3 -I "$HERE/serve.py" "$PORT" > /dev/null 2>&1 &
pid=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if python3 -I -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=1)" "http://127.0.0.1:$PORT/" > /dev/null 2>&1; then break; fi
  sleep 0.2
done

python3 -I - "$PORT" "$HERE" "$LIB" <<'PY'
import json, os, subprocess, sys, urllib.error, urllib.request
from pathlib import Path
port, here, lib = sys.argv[1], sys.argv[2], Path(sys.argv[3])
B = f"http://127.0.0.1:{port}"
bad = 0
def check(name, cond):
    global bad
    print(("PASS  " if cond else "FAIL  ") + name); bad |= not cond
def req(path, method="GET", body=None, headers=None):
    r = urllib.request.Request(B + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=3) as x:
            return x.status, x.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
H = {"Content-Type": "application/json", "X-VS": "1"}
env = dict(os.environ, VS_URL=B, SAMPLER_HOME=str(lib))
vs = lambda *a: subprocess.run([sys.executable, "-I", f"{here}/vs.py", *a], capture_output=True, text=True, env=env)

c, b = req("/"); check("app page served", c == 200 and b"Add song" in b)

# other web pages: a DNS-rebinding page sends its own name as Host; a cross-site POST sends its Origin
EV = {"Host": f"attacker.example:{port}"}
c, _ = req("/api/packs", headers=EV); check("GET with a foreign Host refused (403)", c == 403)
c, _ = req("/requests", "POST", b'{"url":"https://x.com/a"}', {**H, **EV}); check("POST with a foreign Host refused (403)", c == 403)
c, _ = req("/control", "POST", b'{"cmd":"say","text":"x"}', {**H, "Origin": "https://attacker.example"})
check("POST with a foreign Origin refused (403)", c == 403)
c, _ = req("/control", "POST", b'{"cmd":"say","text":"x"}', {**H, "Origin": B}); check("POST from the app's own Origin allowed", c == 200)
c, _ = req("/api/packs", headers={"Host": f"localhost:{port}"}); check("Host localhost allowed", c == 200)

# a symlink under packs/ must not expose what it points to
secret = lib / "secret"; secret.mkdir(); (secret / "key.txt").write_text("SECRET")
(lib / "packs" / "leak").symlink_to(secret)
c, b = req("/packs/leak/key.txt"); check("symlink out of packs/ not served", c == 404 and b"SECRET" not in b)
c, b = req("/api/packs"); check("packs listed, .prev- copies hidden", c == 200 and json.loads(b)["packs"] == ["test-pack"])
c, b = req("/packs/test-pack/chops/synth16/synth%2001.wav"); check("a chop wav is served", c == 200 and b[:4] == b"RIFF")
for p in ("/packs/../../../../etc/passwd", "/packs/%2e%2e/%2e%2e/%2e%2e/etc/passwd", "/../../etc/passwd"):
    c, b = req(p); check(f"traversal blocked: {p}", b"root:" not in b)

# command queue + state
c, _ = req("/control", "POST", b'{"cmd":"say","text":"hi"}', {"Content-Type": "application/json"})
check("POST without X-VS header refused (403)", c == 403)
c, _ = req("/control", "POST", b'{"cmd":"zap"}', H); check("unknown command refused (400)", c == 400)
c, _ = req("/control", "POST", b"[1]", H); check("non-object refused (400)", c == 400)
c, _ = req("/control", "POST", b'{"cmd":"say","text":"' + b"x" * 5000 + b'"}', H); check("oversize body refused (413)", c == 413)
out = vs("bank", "R", "vocal16"); check("vs.py bank queues a command", out.returncode == 0 and "'seq'" in out.stdout)
c, b = req("/control?after=0")
check("queue returns it", any(x.get("cmd") == "bank" and x.get("deck") == "R" for x in json.loads(b)["cmds"]))
req("/state", "POST", b'{"bank":"mix16"}', H)
c, b = req("/state"); check("state round-trips", json.loads(b).get("bank") == "mix16")
check("vs.py rejects pad 17", vs("pad", "L", "17").returncode == 2)
check("vs.py rejects deck X", vs("pitch", "X", "2").returncode == 2)
check("vs.py pitch L -3 queues", vs("pitch", "L", "-3").returncode == 0)
for a in (["sync", "R"], ["sync", "off"], ["vol", "L", "0"], ["vol", "R", "100"], ["xfade", "0"], ["xfade", "100"],
          ["bpmx", "R", "x2"], ["bpmx", "L", "half"], ["bpmx", "L", "x1"]):
    check(f"vs.py {' '.join(a)} queues", vs(*a).returncode == 0)
for a in (["sync", "X"], ["sync"], ["sync", "L", "R"], ["vol", "L", "101"], ["vol", "L", "-1"], ["vol", "L", "8.5"],
          ["xfade", "101"], ["xfade", "L", "50"], ["bpmx", "R", "x3"], ["bpmx", "R", "2"]):
    check(f"vs.py rejects {' '.join(a)}", vs(*a).returncode == 2)
c, b = req(f"/control?after=0"); cmds = json.loads(b)["cmds"]
check("queue has sync R, vol, xfade and bpmx as the page reads them",
      {"cmd": "sync", "deck": "R"} in [{k: x[k] for k in ("cmd", "deck") if k in x} for x in cmds]
      and any(x.get("cmd") == "vol" and x.get("value") == 100 for x in cmds)
      and any(x.get("cmd") == "xfade" and x.get("value") == 0 for x in cmds)
      and any(x.get("cmd") == "bpmx" and x.get("mul") == 0.5 for x in cmds))

# song requests: links
ok_url = "https://www.youtube.com/watch?v=abc123&t=30s"
c, b = req("/requests", "POST", json.dumps({"url": ok_url, "deck": "R"}).encode(), H)
check("a YouTube link becomes a request", c == 200 and json.loads(b)["request"]["status"] == "waiting")
for u in ("http://example.com/x", "https://x.com/$(touch /tmp/pwned)", "https://x.com/a b", 'https://x.com/"q', "file:///etc/passwd", ""):
    c, _ = req("/requests", "POST", json.dumps({"url": u}).encode(), H)
    check(f"link refused: {u!r}", c == 400)
c, _ = req("/requests", "POST", json.dumps({"url": ok_url}).encode(), {"Content-Type": "application/json"})
check("request without X-VS refused (403)", c == 403)
out = vs("requests"); check("vs.py requests lists it", out.returncode == 0 and "abc123" in out.stdout)
out = vs("wait", "1"); check("vs.py wait returns a waiting request", out.returncode == 0 and "abc123" in out.stdout)
check("vs.py run on an unknown id exits 2", vs("run", "999").returncode == 2)
c, _ = req("/requests/1", "POST", b'{"status":"hacked"}', H); check("bad status refused", c == 400)
c, b = req("/requests/1", "POST", b'{"status":"done","pack":"../etc"}', H)
check("done with a bad pack name stores no pack", c == 200 and json.loads(b)["request"]["pack"] is None)
check("vs.py run on a done request exits 2", vs("run", "1").returncode == 2)
check("requests saved to disk", "abc123" in (lib / "requests.json").read_text())

# uploads
U = {"X-VS": "1", "Content-Type": "application/octet-stream"}
c, b = req("/upload?name=My%20Song.mp3&deck=L", "POST", b"ID3fake", U)
check("an mp3 upload lands in the inbox", c == 200 and (lib / "inbox" / "my-song.mp3").read_bytes() == b"ID3fake")
c, b = req("/upload?name=My%20Song.mp3", "POST", b"two", U)
check("a second upload with the same name gets a new name", c == 200 and (lib / "inbox" / "my-song-2.mp3").exists())
c, b = req("/upload?name=..%2F..%2Fescape.wav", "POST", b"RIFF", U)
check("upload name can't leave the inbox", c == 200 and (lib / "inbox" / "escape.wav").exists() and not (lib.parent / "escape.wav").exists())
c, _ = req("/upload?name=evil.sh", "POST", b"#!/bin/sh", U); check("non-audio upload refused", c == 400)
c, _ = req("/upload?name=a.wav", "POST", b"RIFF", {"Content-Type": "application/octet-stream"}); check("upload without X-VS refused", c == 403)

# a planted .part symlink must not be written through
outside = lib / "outside.txt"; outside.write_text("keep")
(lib / "inbox" / ".trap.wav.part").symlink_to(outside)
c, b = req("/upload?name=trap.wav", "POST", b"RIFFnew", U)
check("upload skips a planted .part symlink", c == 200 and outside.read_text() == "keep"
      and (lib / "inbox" / "trap-2.wav").read_bytes() == b"RIFFnew")

# concurrent uploads of the same name each get their own file, intact
import threading
bodies = [f"RIFF-{i}-".encode() * 50000 for i in range(8)]
threads = [threading.Thread(target=req, args=("/upload?name=Same.wav", "POST", bd, U)) for bd in bodies]
[t.start() for t in threads]; [t.join() for t in threads]
got = sorted(f.read_bytes() for f in (lib / "inbox").glob("same*.wav"))
check("8 concurrent same-name uploads -> 8 intact files", got == sorted(bodies))

# vs.py run with a chopper that fails marks the request failed (no venv here -> flip.sh exits 1)
c, b = req("/requests", "POST", json.dumps({"url": "https://www.youtube.com/watch?v=zzz"}).encode(), H)
rid = str(json.loads(b)["request"]["id"])
out = subprocess.run([sys.executable, "-I", f"{here}/vs.py", "run", rid], capture_output=True, text=True,
                     env={**env, "VS_VENV": str(lib / "no-venv")})
st = next(r for r in json.loads(req("/requests")[1])["requests"] if str(r["id"]) == rid)
check("vs.py run: a failing chopper marks the request failed", out.returncode == 1 and st["status"] == "failed")
sys.exit(bad)
PY

# the chopper's guards, only if setup.sh has made the Python environment
VENV_PY="$(bash -c '. "$1/config.sh"; echo "$VS_VENV/bin/python"' _ "$HERE")"
# sync math (app/sync.js), the numbers from the real packs: Benga 1.71828 s bars, Postcard 3.13469 s
if command -v node > /dev/null 2>&1; then
  node - "$HERE/app/sync.js" <<'JS' || exit 1
const SM = require(process.argv[2]);
let bad = 0;
const check = (name, ok) => { console.log((ok ? "PASS  " : "FAIL  ") + name); if (!ok) bad = 1; };
const near = (a, b, e = 1e-9) => Math.abs(a - b) < e;
const onGrid = (t, c0, step) => near((t - c0) / step, Math.round((t - c0) / step), 1e-6);
const benga = 1.71828, postcard = 3.13469;
check("bar = loop_seconds / 8, barlen for old packs, ÷ mul",
      near(SM.bar({ loop_seconds: 8 * benga, barlen: 9 }), benga) && SM.bar({ barlen: 2 }) === 2 && SM.bar({ barlen: 2 }, 2) === 1);
let m = SM.fold(postcard, benga), r = SM.solve(postcard, benga, m);
check(`Postcard follows Benga at half time: m=${m}, rate ${((r - 1) * 100).toFixed(1)}%`, m === 2 && near(r, 0.91216, 1e-5));
check("its real bar is exactly 2 Benga bars", near(postcard / r, 2 * benga));
m = SM.fold(benga, postcard); r = SM.solve(benga, postcard, m);
check(`swapped, Benga follows Postcard at double time: m=${m}, rate +${((r - 1) * 100).toFixed(1)}%`,
      m === 0.5 && near(benga / r, postcard / 2));
check("same tempo folds to 1, rate 1", SM.fold(2, 2) === 1 && SM.solve(2, 2, 1) === 1);
// leader runs from 0; follower joins on the leader's bar line after t=5
const L = { c0: 0, r: 1 }, F = { r: SM.solve(postcard, benga, 2) };
F.c0 = SM.lineAfter(L.c0, benga / L.r, 5);
check("follower starts on a leader bar line", F.c0 >= 5 && onGrid(F.c0, L.c0, benga));
let ok = true;
for (let n = 0; n < 32; n++) ok = ok && onGrid(F.c0 + n * postcard / F.r, L.c0, benga / L.r);
check("32 follower bar lines all sit on leader bar lines (grids nest)", ok);
// leader pitch +2 at t=20.37: both re-anchor at the same instant, follower re-solves with m kept
const now = 20.37, rL = Math.pow(2, 2 / 12);
const rF = SM.solve(postcard, benga / rL, 2);
const posL = (now - L.c0) * L.r, posF = (now - F.c0) * F.r;
L.c0 = SM.reanchor(L.c0, now, L.r, rL); F.c0 = SM.reanchor(F.c0, now, F.r, rF); L.r = rL; F.r = rF;
check("re-anchor keeps the music where it was", near((now - L.c0) * L.r, posL) && near((now - F.c0) * F.r, posF));
ok = true;
for (let n = 0; n < 32; n++) ok = ok && onGrid(F.c0 + n * postcard / F.r, L.c0, benga / L.r);
check("after a leader pitch change the follower's bars still sit on the leader's", ok && near(postcard / F.r, 2 * benga / rL));
check("lineAfter: a time on a line stays on it", SM.lineAfter(0, 2, 4) === 4 && SM.lineAfter(0, 2, 4.001) === 6);
check("loopPos wraps negative times", near(SM.loopPos(10, 9, 1, 4), 3));
check("nearestBar rounds and wraps the loop end to bar 0", SM.nearestBar(7.9, 1, 8) === 0 && SM.nearestBar(2.4, 1, 8) === 2);
process.exit(bad);
JS
  # the page's own script on a fake audio clock: loops on the right bar phase, nothing left sounding
  node "$HERE/tests/sim.js" "$HERE" || exit 1
else
  echo "SKIP  sync math (node not installed)"
fi

if [ -x "$VENV_PY" ]; then
  SAMPLER_HOME="$LIB/chop" "$VENV_PY" -I - "$HERE" "$LIB/chop" <<'PY'
import os, signal, subprocess, sys
from pathlib import Path
import numpy as np, soundfile as sf
here, lib = sys.argv[1], Path(sys.argv[2])
lib.mkdir()
sys.path.insert(0, f"{here}/flippack")
import flippack as fp
fp.check_home()
bad = 0
def check(name, cond):
    global bad
    print(("PASS  " if cond else "FAIL  ") + name); bad |= not cond
def exits(fn, *a, **k):
    try:
        fn(*a, **k)
    except SystemExit as e:
        return str(e.code)
    return ""
def song(path, secs, seed):
    # noise with a click every half second: audible, not silent
    rng = np.random.default_rng(seed)
    x = 0.05 * rng.standard_normal((44100 * secs, 2)).astype("float32")
    x[:: 22050] = 0.9
    sf.write(path, x, 44100)

# every loop is conformed to exactly 8 bars, whatever the detected bar lines gave
n = 44100 * 4
for off in (0.0, 0.01, -0.05, 0.0002):
    x = 0.1 * np.random.default_rng(5).standard_normal((int(n * (1 + off)), 2))
    y, pct = fp.conform(x, 44100, n)
    check(f"conform {off:+.2%} cut -> exactly n samples, stretch {pct:+.2f}%",
          y.shape == (n, 2) and (abs(pct) > 0.05) == (abs(off) > 0.0005) and np.isfinite(y).all())

# vocal chops: 20 one-second sung phrases with 0.4 s gaps. Clean gaps keep the old 30 dB cut;
# gaps only 22 dB down (reverb, bleed: send-me-a-postcard gave 1 chop) step down until 16 fit
def vox(gap_rms, seed=7):
    t = np.arange(fp.SR) / fp.SR
    gap = gap_rms * np.random.default_rng(seed).standard_normal(int(0.4 * fp.SR))
    return np.concatenate([np.concatenate([0.5 * np.sin(2 * np.pi * 220 * t), gap]) for _ in range(20)]).astype("float32")
for gap_rms, want in ((0.0, 30), (0.5 / np.sqrt(2) * 10 ** (-22 / 20), 20)):   # sine RMS = 0.5/sqrt(2)
    got, db = fp.vocal_pieces(vox(gap_rms), fp.SR)
    starts = [c[1] for c in got]
    check(f"vocal chops, gaps {'silent' if not gap_rms else '22 dB down'}: {len(got)} phrases at {db} dB (want 16 at {want})",
          len(got) == 16 and db == want and starts == sorted(starts) and all(0.9 < c[2] - c[1] < 1.1 for c in got))
got, db = fp.vocal_pieces(np.full(fp.SR * 20, 0.3, "float32"), fp.SR)
check("vocal with no gaps at all: no chops, no crash", got == [])

sf.write(lib / "silent.wav", np.zeros((44100 * 30, 2), "float32"), 44100)
check("silent audio refused before splitting", "silent" in exits(fp.check_audio, lib / "silent.wav"))
song(lib / "short.wav", 5, 0)
check("5 s clip refused as too short", "at least 20 s" in exits(fp.check_audio, lib / "short.wav"))
signal.alarm(60)    # if the zero-tempo loop comes back, fail instead of hanging the suite
z = np.zeros(fp.SR * 30, "float32")
check("no beat -> clean exit, not an endless loop", exits(fp.beat_grid, z, z) != "")
signal.alarm(0)

# a dropped file: copied into songs/, the upload itself stays put (so a retry still finds it)
up = fp.INBOX / "up.wav"; song(up, 30, 1)
ida = fp.file_identity(up)
s1 = fp.choose_slug("up", ida)
t = fp.prepare_wav(s1, up, downloaded=False)
check("dropped file -> songs/up.wav, upload left in inbox", s1 == "up" and t == fp.SONGS / "up.wav" and up.exists())
check("same song again reuses its slug", fp.choose_slug("up", ida) == "up")
other = fp.INBOX / "up-x.wav"; song(other, 30, 2)
idb = fp.file_identity(other)
check("different song, same name -> up-2", fp.choose_slug("up", idb) == "up-2")
(fp.PACKS / "old").mkdir()
check("a pack with no .source is never reused", fp.choose_slug("old", "id:c") == "old-2")

# explicit --slug: refuses another song's slug; --force moves the old audio aside
msg = exits(fp.choose_slug, "whatever", idb, slug_arg="up")
check("--slug on another song's slug refused", "already holds another song" in msg)
s2 = fp.choose_slug("whatever", idb, slug_arg="up", force=True)
check("--slug --force takes it, old audio kept as .prev",
      s2 == "up" and not (fp.SONGS / "up.wav").exists() and any(fp.SONGS.glob("up.wav.prev-*"))
      and (fp.SONGS / "up.source").read_text().strip() == idb)

# 4 runs at once, 4 different songs with the same title -> 4 different slugs
code = ("import sys; sys.path.insert(0, sys.argv[1] + '/flippack'); import flippack as fp; "
        "print(fp.choose_slug('race', sys.argv[2]))")
procs = [subprocess.Popen([sys.executable, "-I", "-c", code, here, f"id:{i}"], stdout=subprocess.PIPE, text=True,
                          env={**os.environ, "SAMPLER_HOME": str(lib)}) for i in range(4)]
got = sorted(p.communicate()[0].strip() for p in procs)
check("4 concurrent songs, one title -> 4 slugs", got == ["race", "race-2", "race-3", "race-4"])

# a forced replacement moves the old pack aside too, so a failed replacement + retry can't load it
(fp.PACKS / "up").mkdir()
fp.choose_slug("whatever", "id:new", slug_arg="up", force=True)
check("--force moves the old pack aside", not (fp.PACKS / "up").exists() and any(fp.PACKS.glob("up.prev-*")))

# a process holding a slug's lock stands in for a chop that is running on it
import time
HOLD = ("import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
        "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); sys.stdin.read()")
def holder(slug):
    h = subprocess.Popen([sys.executable, "-I", "-c", HOLD, str(fp.SONGS / f".{slug}.lock")],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    h.stdout.readline()
    return h
CLAIM = ("import sys; sys.path.insert(0, sys.argv[1] + '/flippack'); import flippack as fp; "
         "print(fp.claim_slug(sys.argv[2], sys.argv[3], sys.argv[4] or None, sys.argv[5] == '1'))")
def claim(title, ident, slug_arg="", force=False):
    return subprocess.Popen([sys.executable, "-I", "-c", CLAIM, here, title, ident, slug_arg, "1" if force else "0"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "SAMPLER_HOME": str(lib)})

# --slug --force waits for the running chop instead of moving its files mid-run
song(fp.SONGS / "up.wav", 30, 3)
h = holder("up")
b = claim("x", "id:z", "up", True)
time.sleep(1.5)
check("--force waits while a chop holds the slug", b.poll() is None and (fp.SONGS / "up.wav").exists())
h.stdin.close(); h.wait()
out, _ = b.communicate(timeout=30)
check("... then replaces it", out.strip() == "up" and not (fp.SONGS / "up.wav").exists()
      and (fp.SONGS / "up.source").read_text().strip() == "id:z")

# a run that chose a slug, then lost it to a --force run before getting the lock, stops
# race's owner is whichever of the 4 concurrent runs got there first, so read it rather than assume
h = holder("race")
c = claim("race", (fp.SONGS / "race.source").read_text().strip())
time.sleep(1.5)
(fp.SONGS / "race.source").write_text("id:someone-else\n")
h.stdin.close(); h.wait()
_, err = c.communicate(timeout=30)
check("slug taken by --force while waiting -> stops", c.returncode != 0 and "given to another song" in err)
sys.exit(bad)
PY
else
  echo "SKIP  chopper guards (no Python environment yet: ./setup.sh)"
fi
echo "ALL GREEN"
