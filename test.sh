#!/bin/bash
# Server contract check (~3 s), no songs needed: builds a one-chop fake pack in a temp library,
# then checks files, path safety, the command queue, state, song requests and uploads.
# If setup.sh has been run, it also checks the chopper's guards (bad audio, name clashes; ~10 s).
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
c, b = req("/api/packs"); check("packs listed", c == 200 and json.loads(b)["packs"] == ["test-pack"])
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
if [ -x "$VENV_PY" ]; then
  SAMPLER_HOME="$LIB/chop" "$VENV_PY" -I - "$HERE" "$LIB/chop" <<'PY'
import sys
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
def exits(fn, *a):
    try:
        fn(*a)
    except SystemExit as e:
        return str(e.code)
    return ""
sf.write(lib / "silent.wav", np.zeros((44100 * 30, 2), "float32"), 44100)
check("silent audio refused before splitting", "silent" in exits(fp.check_audio, lib / "silent.wav"))
t = np.arange(44100 * 5) / 44100
sf.write(lib / "short.wav", (0.3 * np.sin(2 * np.pi * 220 * t)).astype("float32"), 44100)
check("5 s clip refused as too short", "at least 20 s" in exits(fp.check_audio, lib / "short.wav"))
z = np.zeros(fp.SR * 30, "float32")
check("no beat -> clean exit, not an endless loop", exits(fp.beat_grid, z, z) != "")
a = fp.pick_slug("song", "id:a")
check("first song gets its name", a == "song")
check("same song again reuses it", fp.pick_slug("song", "id:a") == "song")
check("different song, same name -> song-2", fp.pick_slug("song", "id:b") == "song-2")
(fp.PACKS / "old").mkdir()
check("a pack with no .source is never reused", fp.pick_slug("old", "id:c") == "old-2")
sys.exit(bad)
PY
else
  echo "SKIP  chopper guards (no Python environment yet: ./setup.sh)"
fi
echo "ALL GREEN"
