#!/usr/bin/env python3
"""Local server for the virtual sampler.

  /            the app (app/)
  /packs/...   flip packs from the Crucial drive, read-only
  /api/packs   list of packs that have an analysis.json
  /control     command queue: Mack POSTs commands, the app polls them (GET ?after=<seq>)
  /state       the app POSTs what it is doing; Mack GETs it

Binds 127.0.0.1 only. POSTs must carry the X-VS header: a web page on another site can't add a
custom header to a cross-origin request without a CORS preflight this server never approves, so
it can't drive the sampler.
"""
import json
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP = Path(__file__).resolve().parent / "app"
PACKS = Path("/Volumes/Crucial Media/sampler/packs")
PORT = 8765
COMMANDS = {"bank", "pad", "load", "stop", "say"}
MAX_BODY = 4096

lock = threading.Lock()
queue = []          # [(seq, cmd dict)], newest last, capped
seq = 0
state = {}


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        p = urlparse(path).path
        # SimpleHTTPRequestHandler drops ".." parts and joins onto self.directory, so requests
        # stay inside APP or PACKS
        if p.startswith("/packs/"):
            self.directory = str(PACKS)
            return super().translate_path(p[len("/packs"):])
        self.directory = str(APP)
        return super().translate_path(p)

    def log_message(self, format, *args):  # silence per-request logging (the app polls 6x a second)
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/packs":
            packs = sorted(d.name for d in PACKS.iterdir()
                           if d.is_dir() and not d.name.startswith((".", "_")) and (d / "analysis.json").exists())
            return self.send_json({"packs": packs})
        if u.path == "/control":
            try:
                after = int(parse_qs(u.query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            with lock:
                return self.send_json({"seq": seq, "cmds": [c for s, c in queue if s > after]})
        if u.path == "/state":
            with lock:
                return self.send_json(state)
        return super().do_GET()

    def read_json(self):
        if self.headers.get("X-VS") != "1":
            self.send_json({"error": "missing X-VS header"}, 403)
            return None
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            self.send_json({"error": "body size"}, 413)
            return None
        try:
            obj = json.loads(self.rfile.read(n))
        except ValueError:
            self.send_json({"error": "bad json"}, 400)
            return None
        if not isinstance(obj, dict):
            self.send_json({"error": "expected an object"}, 400)
            return None
        return obj

    def do_POST(self):
        global seq, state
        path = urlparse(self.path).path
        if path not in ("/control", "/state"):
            return self.send_json({"error": "not found"}, 404)
        obj = self.read_json()
        if obj is None:
            return
        with lock:
            if path == "/state":
                state = obj
                return self.send_json({"ok": True})
            if obj.get("cmd") not in COMMANDS:
                return self.send_json({"error": f"cmd must be one of {sorted(COMMANDS)}"}, 400)
            seq += 1
            obj["seq"] = seq
            queue.append((seq, obj))
            del queue[:-200]
            return self.send_json({"ok": True, "seq": seq})


if __name__ == "__main__":
    if not PACKS.is_dir():
        sys.exit(f"packs folder not found: {PACKS} (is Crucial Media mounted?)")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"virtual sampler: http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()
