#!/bin/bash
# Server contract check (~3 s), no songs needed: builds a one-chop fake pack in a temp library,
# then checks files, path safety, the command queue, state, song requests and uploads.
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
sys.exit(bad)
PY
echo "ALL GREEN"
