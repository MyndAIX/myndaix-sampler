#!/usr/bin/env python3
"""Local server for the sampler.

  /                the app (app/)
  /packs/...       packs from $SAMPLER_HOME/packs, read-only
  /api/packs       packs that have an analysis.json
  /control         command queue: an AI agent POSTs commands (vs.py), the app polls them (GET ?after=<seq>)
  /state           the app POSTs what it is doing; the agent GETs it
  /requests        songs the player asked for (a link or a dropped file); the agent chops them (vs.py run)
  /requests/<id>   the agent POSTs progress: working, done (with the pack), failed
  /upload          the app POSTs a dropped audio file (?name=&deck=); it lands in $SAMPLER_HOME/inbox/

Binds 127.0.0.1 only. Three guards keep other web pages out:
  - every request must name this server in its Host header (127.0.0.1 or localhost + port). A page
    that DNS-rebinds its own hostname to 127.0.0.1 still sends its own name as Host, so it's refused.
  - POSTs must carry the X-VS header: a cross-origin page can't add a custom header without a CORS
    preflight this server never approves.
  - a POST whose Origin header names another site is refused.
"""
import json
import os
import re
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parent
APP = REPO / "app"
DEFAULT_HOME = REPO / "library"
HOME = Path(os.environ.get("SAMPLER_HOME") or DEFAULT_HOME)
PACKS, INBOX = HOME / "packs", HOME / "inbox"
REQ_FILE = HOME / "requests.json"
PORT = 8765
COMMANDS = {"bank", "pad", "load", "stop", "say", "pitch"}
MAX_BODY = 4096
MAX_UPLOAD = 300 * 1024 * 1024
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".aif", ".aiff", ".ogg"}
# https only; no quotes, spaces, $ or backticks. The link only ever reaches yt-dlp as one argv item.
URL_RE = re.compile(r"^https://[^\s\"'`$\\<>|;(){}]{1,500}$")
STATUSES = {"working", "done", "failed"}

lock = threading.Lock()
queue = []          # [(seq, cmd dict)], newest last, capped
seq = 0
state = {}
requests = []       # song requests, persisted to REQ_FILE so a restart doesn't lose them


def save_requests():
    tmp = REQ_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(requests[-100:], indent=1))
    tmp.replace(REQ_FILE)           # atomic rename


def add_request(kind, src, deck):
    req = {"id": (requests[-1]["id"] + 1) if requests else 1, "kind": kind, "src": src,
           "deck": deck if deck in ("L", "R") else "L", "status": "waiting", "note": "", "pack": None,
           "t": int(time.time())}
    requests.append(req)
    save_requests()
    return req


def safe_name(name):
    p = Path(name)
    ext = p.suffix.lower()
    stem = re.sub(r"[^a-z0-9]+", "-", p.stem.lower()).strip("-")[:60].strip("-") or "song"
    return stem, ext


def inside(path, root):
    # resolves symlinks: a link under packs/ that points elsewhere is outside
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        p = urlparse(path).path
        # SimpleHTTPRequestHandler drops ".." parts and joins onto self.directory; inside() then
        # rejects anything a symlink would take out of APP or PACKS
        if p.startswith("/packs/"):
            self.directory = str(PACKS)
            out = super().translate_path(p[len("/packs"):])
        else:
            self.directory = str(APP)
            out = super().translate_path(p)
        return out if inside(out, self.directory) else os.path.join(self.directory, ".denied", "404")  # a path that never exists -> 404

    def host_ok(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "").lower() not in allowed:
            self.send_json({"error": "wrong Host: open the sampler at http://127.0.0.1:%d/" % port}, 403)
            return False
        return True

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

    def do_HEAD(self):
        if self.host_ok():
            super().do_HEAD()

    def do_GET(self):
        if not self.host_ok():
            return
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
        if u.path == "/requests":
            with lock:
                return self.send_json({"requests": requests})
        return super().do_GET()

    def guard(self):
        if not self.host_ok():
            return False
        if self.headers.get("X-VS") != "1":
            self.send_json({"error": "missing X-VS header"}, 403)
            return False
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        if origin is not None and origin.lower() not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self.send_json({"error": "foreign Origin"}, 403)
            return False
        return True

    def read_json(self):
        if not self.guard():
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

    def upload(self, q):
        if not self.guard():
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_UPLOAD:
            return self.send_json({"error": f"file must be under {MAX_UPLOAD // 2**20} MB"}, 413)
        stem, ext = safe_name(q.get("name", [""])[0])
        if ext not in AUDIO_EXT:
            return self.send_json({"error": f"audio files only: {' '.join(sorted(AUDIO_EXT))}"}, 400)
        INBOX.mkdir(parents=True, exist_ok=True)
        # The .part file is the reservation: O_EXCL makes two concurrent uploads of the same name
        # pick different names, and O_NOFOLLOW refuses a planted symlink instead of writing through it.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        k = 1
        while True:
            dest = INBOX / (f"{stem}{ext}" if k == 1 else f"{stem}-{k}{ext}")
            part = dest.with_name(f".{dest.name}.part")
            k += 1
            if os.path.lexists(dest):
                continue
            try:
                fd = os.open(part, flags, 0o644)
            except FileExistsError:
                continue
            # an upload that held this .part may have finished (renamed it to dest) just before
            # our open, so the name is taken after all
            if os.path.lexists(dest):
                os.close(fd)
                part.unlink(missing_ok=True)
                continue
            break
        try:
            with os.fdopen(fd, "wb") as f:
                left = n
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ConnectionError("upload cut off")
                    f.write(chunk)
                    left -= len(chunk)
            if os.path.lexists(dest):
                raise FileExistsError(f"{dest.name} appeared during the upload")
            part.replace(dest)
        except Exception as e:
            part.unlink(missing_ok=True)
            return self.send_json({"error": str(e)}, 400)
        with lock:
            req = add_request("file", dest.name, q.get("deck", ["L"])[0])
        return self.send_json({"ok": True, "request": req})

    def do_POST(self):
        global seq, state
        u = urlparse(self.path)
        path = u.path
        if path == "/upload":
            return self.upload(parse_qs(u.query))
        m = re.fullmatch(r"/requests/(\d+)", path)
        if path not in ("/control", "/state", "/requests") and not m:
            return self.send_json({"error": "not found"}, 404)
        obj = self.read_json()
        if obj is None:
            return
        with lock:
            if path == "/state":
                state = obj
                return self.send_json({"ok": True})
            if path == "/requests":
                url = str(obj.get("url", "")).strip()
                if not URL_RE.match(url):
                    return self.send_json({"error": "paste a full https:// link"}, 400)
                return self.send_json({"ok": True, "request": add_request("link", url, obj.get("deck"))})
            if m:
                req = next((r for r in requests if r["id"] == int(m.group(1))), None)
                if req is None:
                    return self.send_json({"error": "no such request"}, 404)
                if obj.get("status") not in STATUSES:
                    return self.send_json({"error": f"status must be one of {sorted(STATUSES)}"}, 400)
                req["status"] = obj["status"]
                req["note"] = str(obj.get("note", ""))[:300]
                pack = obj.get("pack")
                req["pack"] = pack if isinstance(pack, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", pack) else None
                save_requests()
                return self.send_json({"ok": True, "request": req})
            if obj.get("cmd") not in COMMANDS:
                return self.send_json({"error": f"cmd must be one of {sorted(COMMANDS)}"}, 400)
            seq += 1
            obj["seq"] = seq
            queue.append((seq, obj))
            del queue[:-200]
            return self.send_json({"ok": True, "seq": seq})


if __name__ == "__main__":
    # A SAMPLER_HOME that is missing is usually an unplugged drive. Creating it would write the
    # library onto the internal disk under /Volumes, so only the default library is ever created.
    if HOME != DEFAULT_HOME and not HOME.is_dir():
        sys.exit(f"SAMPLER_HOME {HOME} does not exist (is the drive plugged in?)")
    PACKS.mkdir(parents=True, exist_ok=True)
    if REQ_FILE.exists():
        try:
            requests = json.loads(REQ_FILE.read_text())
        except ValueError:
            REQ_FILE.rename(REQ_FILE.with_suffix(f".bad-{int(time.time())}"))
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"sampler: http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()
