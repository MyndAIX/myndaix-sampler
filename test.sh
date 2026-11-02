#!/bin/bash
# Server contract check (~2 s): files, path safety, the command queue, state.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT=8766
python3 -I "$HERE/serve.py" "$PORT" > /dev/null 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null' EXIT INT TERM
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if python3 -I -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=1)" "http://127.0.0.1:$PORT/" > /dev/null 2>&1; then break; fi
  sleep 0.2
done
python3 -I - "$PORT" "$HERE" <<'PY'
import json, os, subprocess, sys, urllib.error, urllib.request
port, here = sys.argv[1], sys.argv[2]
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
c, b = req("/"); check("app page served", c == 200 and b"VIRTUAL SAMPLER" in b)
c, b = req("/api/packs"); packs = json.loads(b)["packs"]
check(f"packs listed ({packs})", c == 200 and "bronski-beat-smalltown-boy" in packs)
c, b = req("/packs/bronski-beat-smalltown-boy/chops/synth16/synth%2001.wav")
check("a chop wav is served", c == 200 and b[:4] == b"RIFF")
for p in ("/packs/../../../../etc/passwd", "/packs/%2e%2e/%2e%2e/%2e%2e/etc/passwd", "/../../etc/passwd"):
    c, b = req(p); check(f"traversal blocked: {p}", b"root:" not in b)
H = {"Content-Type": "application/json", "X-VS": "1"}
c, _ = req("/control", "POST", b'{"cmd":"say","text":"hi"}', {"Content-Type": "application/json"})
check("POST without X-VS header refused (403)", c == 403)
c, _ = req("/control", "POST", b'{"cmd":"zap"}', H); check("unknown command refused (400)", c == 400)
c, _ = req("/control", "POST", b"[1]", H); check("non-object refused (400)", c == 400)
c, _ = req("/control", "POST", b'{"cmd":"say","text":"' + b"x" * 5000 + b'"}', H); check("oversize body refused (413)", c == 413)
env = dict(os.environ, VS_URL=B)
out = subprocess.run([sys.executable, "-I", f"{here}/vs.py", "bank", "vocal16"], capture_output=True, text=True, env=env)
check("vs.py bank queues a command", out.returncode == 0 and "'seq'" in out.stdout)
c, b = req("/control?after=0"); cmds = json.loads(b)["cmds"]
check("queue returns it", any(x.get("cmd") == "bank" and x.get("name") == "vocal16" for x in cmds))
req("/state", "POST", b'{"bank":"mix16"}', H)
c, b = req("/state"); check("state round-trips", json.loads(b).get("bank") == "mix16")
out = subprocess.run([sys.executable, "-I", f"{here}/vs.py", "pad", "17"], capture_output=True, text=True, env=env)
check("vs.py rejects pad 17", out.returncode == 2)
sys.exit(bad)
PY
echo "ALL GREEN"
