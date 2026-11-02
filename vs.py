#!/usr/bin/env python3
"""The AI agent's side of the sampler: chop songs the player asks for, play along, read state.
DECK is L (left) or R (right). The sampler must be running (./run.sh).

Songs the player asked for (a pasted link or a dropped file):
  vs.py requests               list them with their status
  vs.py wait [SECONDS]         block until a request is waiting (default 600 s); prints it
  vs.py run ID                 download if needed, chop, and load it onto the deck it was asked for

Playing along:
  vs.py state                  what each deck has loaded, banks, pitch, recent pad hits (who hit them)
  vs.py bank DECK vocal16      switch a deck's bank (loops, synth16, mix16, vocal16)
  vs.py pad DECK 5             hit a pad (1-16, pad 1 bottom-left)
  vs.py pitch DECK -3          set a deck's pitch in semitones (-12..12; speed moves with it)
  vs.py load DECK <pack-slug>  load another pack on a deck
  vs.py stop                   stop all loops on both decks
  vs.py say "text"             show a message on the sampler screen
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("VS_URL", "http://127.0.0.1:8765")
REPO = Path(__file__).resolve().parent
# same rule as serve.py
URL_RE = re.compile(r"^https://[^\s\"'`$\\<>|;(){}]{1,500}$")


def call(method, path, obj=None):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-VS": "1"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def send(obj):
    print(call("POST", "/control", obj))
    return 0


def waiting():
    return [r for r in call("GET", "/requests")["requests"] if r["status"] == "waiting"]


def sampler_home():
    # flip.sh resolves SAMPLER_HOME the same way (config.sh); ask it rather than re-implement it
    out = subprocess.run(["bash", "-c", '. "$1/config.sh" && printf %s "$SAMPLER_HOME"', "_", str(REPO)],
                         capture_output=True, text=True, check=True)
    return Path(out.stdout)


def run(rid):
    req = next((r for r in call("GET", "/requests")["requests"] if r["id"] == rid), None)
    if req is None:
        print(f"no request {rid}", file=sys.stderr)
        return 2
    if req["status"] in ("working", "done"):
        print(f"request {rid} is already {req['status']}", file=sys.stderr)
        return 2
    # The request text came from the browser: it is only ever passed as one argv item, never
    # through a shell, and it is checked again here.
    if req["kind"] == "link":
        if not URL_RE.match(req["src"]):
            return fail(rid, "link rejected")
        src = req["src"]
    else:
        inbox = (sampler_home() / "inbox").resolve()
        p = (inbox / req["src"]).resolve()
        if p.parent != inbox or not p.is_file():
            return fail(rid, "file not found in the inbox")
        src = str(p)
    call("POST", f"/requests/{rid}", {"status": "working", "note": "chopping (a few minutes)"})
    call("POST", "/control", {"cmd": "say", "text": f"chopping song #{rid}, this takes a few minutes"})
    proc = subprocess.Popen([str(REPO / "flip.sh"), src], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line.rstrip("\n"))
    proc.wait()
    slug = None
    if proc.returncode == 0:
        try:
            slug = Path(json.loads(lines[-1])["pack"]).name
        except (IndexError, ValueError, KeyError):
            pass
    else:
        # already chopped earlier: load that pack instead of failing
        for ln in lines:
            m = re.match(r"^(.*) exists; pass --force", ln)
            if m:
                slug = Path(m.group(1)).name
    if not slug:
        return fail(rid, (lines[-1] if lines else "flip.sh failed")[:200])
    call("POST", f"/requests/{rid}", {"status": "done", "pack": slug, "note": ""})
    call("POST", "/control", {"cmd": "load", "deck": req["deck"], "pack": slug})
    call("POST", "/control", {"cmd": "say", "text": f"{slug} is on the {'left' if req['deck'] == 'L' else 'right'} deck"})
    print(f"done: {slug} loaded on deck {req['deck']}")
    return 0


def fail(rid, note):
    call("POST", f"/requests/{rid}", {"status": "failed", "note": note})
    call("POST", "/control", {"cmd": "say", "text": f"song #{rid} failed: {note}"})
    print(f"failed: {note}", file=sys.stderr)
    return 1


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "state" and not rest:
        print(json.dumps(call("GET", "/state"), indent=1))
        return 0
    if cmd == "requests" and not rest:
        print(json.dumps(call("GET", "/requests")["requests"], indent=1))
        return 0
    if cmd == "wait" and len(rest) <= 1 and all(a.isdigit() for a in rest):
        end = time.time() + (int(rest[0]) if rest else 600)
        while time.time() < end:
            w = waiting()
            if w:
                print(json.dumps(w, indent=1))
                return 0
            time.sleep(2)
        print("no requests yet", file=sys.stderr)
        return 1
    if cmd == "run" and len(rest) == 1 and rest[0].isdigit():
        return run(int(rest[0]))
    if cmd == "stop" and not rest:
        return send({"cmd": "stop"})
    if cmd == "say" and len(rest) == 1:
        return send({"cmd": "say", "text": rest[0]})
    if len(rest) == 2 and rest[0] in ("L", "R"):
        deck, arg = rest
        if cmd == "bank":
            return send({"cmd": "bank", "deck": deck, "name": arg})
        if cmd == "load":
            return send({"cmd": "load", "deck": deck, "pack": arg})
        if cmd == "pad" and arg.isdigit() and 1 <= int(arg) <= 16:
            return send({"cmd": "pad", "deck": deck, "pad": int(arg)})
        if cmd == "pitch" and re.fullmatch(r"[+-]?\d{1,2}", arg) and -12 <= int(arg) <= 12:
            return send({"cmd": "pitch", "deck": deck, "semis": int(arg)})
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
