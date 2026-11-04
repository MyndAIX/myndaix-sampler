#!/usr/bin/env python3
"""Local server for the sampler.

  /                the app (app/)
  /packs/...       packs from $SAMPLER_HOME/packs, read-only
  /api/packs       packs that have an analysis.json
  /control         command queue: an AI agent POSTs commands (vs.py), the app polls them (GET ?after=<seq>)
  /state           the app POSTs what it is doing; the agent GETs it
  /requests        songs the player asked for (a link or a dropped file). The server's chop worker runs
                   them one at a time (vs.py run <id>); SAMPLER_AUTOCHOP=0 leaves them for an agent
  /requests/<id>   vs.py run POSTs progress: working, done (with the pack), failed
  /upload          the app POSTs a dropped audio file (?name=&deck=); it lands in $SAMPLER_HOME/inbox/
  /stems/<slug>/<stem>.wav   a whole stem (or mix), read-only, for the chop editor
  /api/mine/<slug>           the player's own bank for a pack: owner, rev, pads, which stems can be cut
  /mine/<slug>/<file>        one of those pads' WAVs (only files the current manifest names)
  /api/mine        POST: cut a span of a stem onto a pad of the player's bank, or clear a pad

Binds 127.0.0.1 only. Three guards keep other web pages out:
  - every request must name this server in its Host header (127.0.0.1 or localhost + port). A page
    that DNS-rebinds its own hostname to 127.0.0.1 still sends its own name as Host, so it's refused.
  - POSTs must carry the X-VS header: a cross-origin page can't add a custom header without a CORS
    preflight this server never approves.
  - a POST whose Origin header names another site is refused.
"""
import array
import fcntl
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

REPO = Path(__file__).resolve().parent
APP = REPO / "app"
DEFAULT_HOME = REPO / "library"
HOME = Path(os.environ.get("SAMPLER_HOME") or DEFAULT_HOME)
PACKS, INBOX = HOME / "packs", HOME / "inbox"
SONGS, STEMS, MINE = HOME / "songs", HOME / "stems" / "htdemucs_6s", HOME / "mine"
STEM_NAMES = ("vocals", "drums", "bass", "other", "guitar", "piano", "mix")
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")      # no dots: so never a .prev- copy, never ..
MIN_CUT, MAX_CUT, FADE_S = 0.05, 32.0, 0.003
REQ_FILE = HOME / "requests.json"
PORT = 8765
COMMANDS = {"bank", "pad", "load", "stop", "say", "pitch", "sync", "vol", "xfade", "bpmx"}
MAX_BODY = 4096
MAX_UPLOAD = 300 * 1024 * 1024
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".aif", ".aiff", ".ogg"}
# https only; no quotes, spaces, $ or backticks. The link only ever reaches yt-dlp as one argv item.
URL_RE = re.compile(r"^https://[^\s\"'`$\\<>|;(){}]{1,500}$")
STATUSES = {"working", "done", "failed"}

AUTOCHOP = os.environ.get("SAMPLER_AUTOCHOP", "1") != "0"

lock = threading.Lock()
wake = threading.Event()   # set when a request is added: the chop worker looks at once
queue = []          # [(seq, cmd dict)], newest last, capped
seq = 0
state = {}
requests = []       # song requests, persisted to REQ_FILE so a restart doesn't lose them


def save_requests():
    tmp = REQ_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(requests[-100:], indent=1))
    tmp.replace(REQ_FILE)           # atomic rename


def add_request(kind, src, deck):
    deck = deck if deck in ("L", "R") else "L"
    # the same song for the same deck already queued or chopping: that request, not a second one
    # (pressing add again while nothing seemed to happen used to queue it four times)
    for r in requests:
        if r["kind"] == kind and r["src"] == src and r["deck"] == deck and r["status"] in ("waiting", "working"):
            return r
    req = {"id": (requests[-1]["id"] + 1) if requests else 1, "kind": kind, "src": src,
           "deck": deck, "status": "waiting", "note": "", "pack": None,
           "t": int(time.time())}
    requests.append(req)
    save_requests()
    wake.set()
    return req


def chop_worker(port):
    """Runs waiting requests one at a time, oldest first, through `vs.py run <id>`: the same path an
    agent uses, so the link check, the progress posts and the deck load are one piece of code. One at
    a time on purpose: two stem splits at once would only slow both down. vs.py marks the request
    working itself (it refuses one already marked), so the worker never claims it first."""
    env = dict(os.environ, VS_URL=f"http://127.0.0.1:{port}", SAMPLER_HOME=str(HOME))
    while True:
        wake.wait(5)
        wake.clear()
        while True:
            with lock:
                nxt = next((r for r in requests if r["status"] == "waiting"), None)
            if nxt is None:
                break
            print(f"[{time.strftime('%H:%M:%S')}] chop worker: song #{nxt['id']}", flush=True)
            subprocess.run([sys.executable, "-I", str(REPO / "vs.py"), "run", str(nxt["id"])],
                           env=env, stdin=subprocess.DEVNULL)
            with lock:          # vs.py died before posting anything: don't pick it again forever
                if nxt["status"] == "waiting":
                    nxt["status"], nxt["note"] = "failed", "the chopper did not start"
                    save_requests()
            print(f"[{time.strftime('%H:%M:%S')}] chop worker: song #{nxt['id']} {nxt['status']}", flush=True)


def safe_name(name):
    p = Path(name)
    ext = p.suffix.lower()
    stem = re.sub(r"[^a-z0-9]+", "-", p.stem.lower()).strip("-")[:60].strip("-") or "song"
    return stem, ext


def inside(path, root):
    # resolves symlinks: a link under packs/ that points elsewhere is outside
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


# ---- the player's own bank ("mine"), cut from whole stems in the chop editor ----
# Kept in mine/<slug>/, outside packs/: a --force re-chop renames packs/<slug> and builds a new one, and
# the player's picks must survive that. regions.json is the record; renaming a new one into place is
# the commit, so a crash leaves either the old bank or the new one. Cut WAVs are never overwritten or
# deleted (each gets a fresh name), so an old pick can always be put back by hand.

def pack_dir(slug):
    """Paths only: is this a current pack? Whether it may be saved to is owner_of's question."""
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        return None
    d = PACKS / slug
    a = d / "analysis.json"
    return d if a.is_file() and inside(d, PACKS) and inside(a, PACKS) else None


def stem_file(slug, name):
    if name not in STEM_NAMES:
        return None
    root = SONGS if name == "mix" else STEMS
    f = SONGS / f"{slug}.wav" if name == "mix" else STEMS / slug / f"{name}.wav"
    return f if f.is_file() and inside(f, root) else None


def owner_of(slug):
    """The song that owns this slug (songs/<slug>.source), or None. flippack writes it under
    songs/.names.lock, so read it under the same lock: never half-written."""
    m = SONGS / f"{slug}.source"
    if not SONGS.is_dir():
        return None
    fd = os.open(SONGS / ".names.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if not (m.is_file() and inside(m, SONGS)):
            return None
        return m.read_text().strip() or None
    finally:
        os.close(fd)


def mine_dir(slug):
    d = MINE / slug
    return d if inside(d, MINE) and not os.path.islink(d) else None


def read_manifest(slug):
    """(bank, None), (None, None) when there is no bank yet, or (None, why) when there is one but it
    can't be trusted: a symlink (it would read a file outside the library) or damaged JSON. Commits are
    atomic, so a damaged one was edited by hand: a save refuses it rather than start over on top."""
    d = mine_dir(slug)
    if d is None:
        return None, "bad mine folder"
    f = d / "regions.json"
    try:
        fd = os.open(f, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, None
    except OSError:                     # ELOOP: regions.json is a symlink
        return None, "mine/<slug>/regions.json is not a plain file"
    try:
        with os.fdopen(fd) as fh:
            m = json.loads(fh.read())
    except (OSError, ValueError):
        return None, "mine/<slug>/regions.json is damaged"
    ok = isinstance(m, dict) and isinstance(m.get("pads"), dict) and type(m.get("rev")) is int
    return (m, None) if ok else (None, "mine/<slug>/regions.json is damaged")


def live_manifest(slug):
    """The bank to show, or None. A missing owner never matches, and a bank made for another song
    under this slug is refused."""
    owner = owner_of(slug)
    m, _ = read_manifest(slug)
    if owner is None or m is None or m.get("owner") != owner:
        return None
    return m


def stem_format(f):
    """'pcm16' if this WAV can be cut here (stdlib wave + array, no numpy), else why not."""
    try:
        with wave.open(str(f), "rb") as w:
            return "pcm16" if w.getsampwidth() == 2 and w.getcomptype() == "NONE" else f"{8 * w.getsampwidth()}-bit"
    except (wave.Error, EOFError, OSError) as e:
        return f"unsupported ({str(e)[:40]})"


def mine_info(slug):
    owner = owner_of(slug)
    m = live_manifest(slug)
    _, bad = read_manifest(slug)
    formats = {n: stem_format(f) for n in STEM_NAMES if (f := stem_file(slug, n))}
    why = ("this pack has no songs/<slug>.source, so it can be viewed but not saved to" if owner is None
           else f"{bad}: move it aside to start a new bank" if bad else None)
    return {"slug": slug, "owner": owner, "rev": m["rev"] if m else 0,
            "pads": m["pads"] if m else {}, "formats": formats,
            "saveable": why is None, "why": why}


def cut_pcm16(src, t0, t1):
    """Frames round(t0*sr)..round(t1*sr) of a 16-bit PCM WAV, never stretched, with a short linear
    fade at each end (the end samples are exactly 0, so the pad never clicks)."""
    with wave.open(str(src), "rb") as w:
        if w.getsampwidth() != 2 or w.getcomptype() != "NONE":
            raise ValueError("this stem is not 16-bit PCM, so it can't be cut here")
        ch, sr, n = w.getnchannels(), w.getframerate(), w.getnframes()
        a, b = round(t0 * sr), round(t1 * sr)
        if not 0 <= a < b <= n:
            raise ValueError(f"span is outside the stem (0-{n / sr:.2f} s)")
        w.setpos(a)
        raw = w.readframes(b - a)
    x = array.array("h")
    x.frombytes(raw)
    if sys.byteorder == "big":          # WAV samples are little-endian
        x.byteswap()
    frames = len(x) // ch
    k = min(int(sr * FADE_S), frames // 2)
    for i in range(k):
        g = i / k
        for c in range(ch):
            x[i * ch + c] = int(x[i * ch + c] * g)
            j = (frames - 1 - i) * ch + c
            x[j] = int(x[j] * g)
    if sys.byteorder == "big":
        x.byteswap()
    return x.tobytes(), ch, sr, a, b


def mkdir_new(d):
    """True if this call made the folder."""
    try:
        d.mkdir()
        return True
    except FileExistsError:
        return False


def fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_cut(d, pad, rev, pcm, ch, sr):
    # O_EXCL + a random suffix: an orphan left by a crash before the commit is never reused or overwritten
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(20):
        name = f"p{pad:02d}-r{rev:04d}-{secrets.token_hex(3)}.wav"
        try:
            fd = os.open(d / name, flags, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            w = wave.open(f, "wb")          # closing a wave writer that was given a file leaves the file open
            w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes(pcm)
            w.close()
            f.flush()
            os.fsync(f.fileno())
        return name
    raise OSError("no free file name")


def commit_manifest(d, m):
    tmp = d / "regions.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(m, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, d / "regions.json")      # the commit
    fsync_dir(d)


def num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def save_mine(obj):
    """POST /api/mine. Returns (http code, reply). Holds the chopper's own slug lock for the whole
    save, so a save never runs while a chop (or a --force replacement) is moving this slug's files."""
    slug = obj.get("slug")
    if pack_dir(slug) is None:
        return 404, {"error": "no such pack"}
    pad = obj.get("pad")
    if not (isinstance(pad, int) and not isinstance(pad, bool) and 1 <= pad <= 16):
        return 400, {"error": "pad must be 1-16"}
    clear = obj.get("clear") is True
    if not clear:
        stem, t0, t1 = obj.get("stem"), num(obj.get("t0")), num(obj.get("t1"))
        if stem not in STEM_NAMES:
            return 400, {"error": f"stem must be one of {list(STEM_NAMES)}"}
        if t0 is None or t1 is None or not 0 <= t0 < t1:
            return 400, {"error": "t0 and t1 must be numbers, 0 <= t0 < t1"}
        if not MIN_CUT <= t1 - t0 <= MAX_CUT:
            return 400, {"error": f"a chop is {MIN_CUT}-{MAX_CUT:g} s long"}
        src = stem_file(slug, stem)
        if src is None:
            return 404, {"error": f"no {stem} stem for this pack"}
    if not SONGS.is_dir():
        return 409, {"error": "this pack has no owner record, so it can't be saved to"}
    # songs/.<slug>.lock is the chopper's per-slug lock (flippack claim_slug). Non-blocking: a chop
    # holds it for minutes, and an HTTP thread must not wait that long.
    lk = os.open(SONGS / f".{slug}.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 409, {"error": "pack busy (a chop or another save is running); try again", "busy": True}
        owner = owner_of(slug)
        if owner is None:
            return 409, {"error": "this pack has no owner record, so it can't be saved to"}
        if obj.get("owner") != owner:
            return 409, {"error": "this pack changed (re-chopped as another song); reload", "stale": True}
        # a new folder's own entry is in its parent: fsync the parent too, or a power cut can lose
        # a save that was already acknowledged
        if mkdir_new(MINE):
            fsync_dir(HOME)
        d = mine_dir(slug)
        if d is None:
            return 400, {"error": "bad mine folder"}
        if mkdir_new(d):
            fsync_dir(MINE)
        if not inside(d, MINE):
            return 400, {"error": "bad mine folder"}
        m, bad = read_manifest(slug)
        if bad:
            return 409, {"error": f"{bad}; move it aside to start a new bank"}
        if m is None:
            m = {"owner": owner, "rev": 0, "pads": {}, "prev": {}}
        elif m.get("owner") != owner:
            return 409, {"error": "the saved bank belongs to another song under this name", "stale": True}
        rev = int(m.get("rev", 0)) + 1
        key = str(pad)
        if clear:
            entry = None
        else:
            try:
                pcm, ch, sr, a, b = cut_pcm16(src, t0, t1)
            except (ValueError, wave.Error, EOFError) as e:
                return 400, {"error": str(e)}
            entry = {"file": write_cut(d, pad, rev, pcm, ch, sr), "stem": stem, "a": a, "b": b, "sr": sr}
            fsync_dir(d)                # the cut's name is on disk before any manifest names it
        pads = dict(m["pads"])
        prev = dict(m.get("prev") or {})
        if key in pads:
            prev[key] = pads[key]           # the pick this replaced or cleared, so it can be put back
        if entry:
            pads[key] = entry
        else:
            pads.pop(key, None)
        commit_manifest(d, {"owner": owner, "rev": rev, "pads": pads, "prev": prev})
    finally:
        os.close(lk)
    notify({"cmd": "mine", "pack": slug, "rev": rev})
    return 200, {"ok": True, "rev": rev, "pad": pad, "entry": entry}


def notify(cmd):
    # a hint on the page's command queue: decks showing this pack refetch /api/mine/<slug>
    global seq
    with lock:
        seq += 1
        cmd["seq"] = seq
        queue.append((seq, cmd))
        del queue[:-200]


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
        if self.command != "HEAD":      # HEAD gets GET's headers and no body, errors included
            self.wfile.write(body)

    def send_file(self, f, ctype, head=False):
        try:
            fh = open(f, "rb")
        except OSError:
            return self.send_json({"error": "not found"}, 404)
        with fh:
            size = os.fstat(fh.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head:
                shutil.copyfileobj(fh, self.wfile, 1 << 20)

    def mine_routes(self, path, head=False):
        """The chop editor's read-only routes. Exact shapes only, no directory listings. True if handled."""
        m = re.fullmatch(r"/stems/([^/]+)/([a-z]+)\.wav", path)
        if m:
            f = stem_file(m.group(1), m.group(2)) if pack_dir(m.group(1)) else None
            if f is None:
                self.send_json({"error": "not found"}, 404)
            else:
                self.send_file(f, "audio/wav", head)
            return True
        m = re.fullmatch(r"/api/mine/([^/]+)", path)
        if m:
            if pack_dir(m.group(1)) is None:
                self.send_json({"error": "no such pack"}, 404)
            else:
                self.send_json(mine_info(m.group(1)))
            return True
        m = re.fullmatch(r"/mine/([^/]+)/([^/]+)", path)
        if m:
            slug, name = m.group(1), m.group(2)
            man = live_manifest(slug) if pack_dir(slug) else None
            # only a file the current bank names: never regions.json, archives or replaced picks
            ok = man is not None and any(isinstance(e, dict) and e.get("file") == name for e in man["pads"].values())
            md = mine_dir(slug) if ok else None
            f = md / name if md else None
            # the manifest is a file on disk: a hand-edited name ("..") or a planted symlink must
            # still not reach outside this bank's folder
            if f is None or not re.fullmatch(r"[A-Za-z0-9._-]+\.wav", name) or not f.is_file() or not inside(f, md):
                self.send_json({"error": "not found"}, 404)
            else:
                self.send_file(f, "audio/wav", head)
            return True
        if path.startswith(("/stems/", "/mine/", "/api/mine")):     # any other shape under these: 404
            self.send_json({"error": "not found"}, 404)
            return True
        return False

    def do_HEAD(self):
        if self.host_ok() and not self.mine_routes(unquote(urlparse(self.path).path), head=True):
            super().do_HEAD()

    def do_GET(self):
        if not self.host_ok():
            return
        u = urlparse(self.path)
        if u.path == "/api/packs":
            packs = sorted(d.name for d in PACKS.iterdir()
                           if d.is_dir() and not d.name.startswith((".", "_")) and ".prev-" not in d.name
                           and (d / "analysis.json").exists())
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
                return self.send_json({"requests": requests, "auto": AUTOCHOP})
        if self.mine_routes(unquote(u.path)):
            return
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
        if path == "/api/mine":
            obj = self.read_json()          # the same Host, X-VS, Origin and size guards as every POST
            if obj is not None:
                code, reply = save_mine(obj)
                self.send_json(reply, code)
            return
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
    # a request still "working" at start-up lost its chopper with the last server: say so, rather
    # than leave it spinning forever (it is not re-run by itself, it may be an old one)
    for r in requests:
        if r.get("status") == "working":
            r["status"], r["note"] = "failed", "interrupted (the server restarted): add it again"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"sampler: http://127.0.0.1:{port}/", flush=True)
    if AUTOCHOP:
        threading.Thread(target=chop_worker, args=(port,), daemon=True).start()
    srv.serve_forever()
