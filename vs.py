#!/usr/bin/env python3
"""Mack's side of the sampler: send a command, or read what the app is doing.
DECK is L (left) or R (right).

  vs.py state                  what each deck has loaded, banks, pitch, recent pad hits
  vs.py bank DECK vocal16      switch a deck's bank (loops, synth16, mix16, vocal16)
  vs.py pad DECK 5             hit a pad (1-16, pad 1 bottom-left)
  vs.py pitch DECK -3          set a deck's pitch in semitones (-12..12; speed moves with it)
  vs.py load DECK <pack-slug>  load another flip pack on a deck
  vs.py stop                   stop all loops on both decks
  vs.py say "text"             show a message on the sampler screen
"""
import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("VS_URL", "http://127.0.0.1:8765")


def call(method, path, obj=None):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-VS": "1"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def send(obj):
    print(call("POST", "/control", obj))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "state" and not rest:
        print(json.dumps(call("GET", "/state"), indent=1))
        return 0
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
