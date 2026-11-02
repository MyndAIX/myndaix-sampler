#!/usr/bin/env python3
"""Mack's side of the sampler: send a command, or read what the app is doing.

  vs.py state                  what is loaded, which bank, recent pad hits
  vs.py bank vocal16           switch the pads to a bank (loops, synth16, mix16, vocal16)
  vs.py pad 5                  hit pad 5 (1-16, pad 1 bottom-left)
  vs.py load <pack-slug>       load another flip pack
  vs.py stop                   stop all loops
  vs.py say "text"             show a message on the sampler screen
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("VS_URL", "http://127.0.0.1:8765")


def call(method, path, obj=None):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-VS": "1"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "state":
        print(json.dumps(call("GET", "/state"), indent=1))
        return 0
    if cmd in ("bank", "load", "say") and len(rest) == 1:
        key = {"bank": "name", "load": "pack", "say": "text"}[cmd]
        print(call("POST", "/control", {"cmd": cmd, key: rest[0]}))
        return 0
    if cmd == "pad" and len(rest) == 1 and rest[0].isdigit() and 1 <= int(rest[0]) <= 16:
        print(call("POST", "/control", {"cmd": "pad", "pad": int(rest[0])}))
        return 0
    if cmd == "stop" and not rest:
        print(call("POST", "/control", {"cmd": "stop"}))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
