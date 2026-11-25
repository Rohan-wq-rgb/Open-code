#!/usr/bin/env python3
"""
Open Code local agent  (Python 3.9+, standard library only)

Runs shell commands for the Open Code assistant on YOUR computer, inside ONE project
folder. It is a separate program on purpose: commands never run on the Render server,
which holds your API key and password.

    python agent.py --dir . --origin https://your-app.onrender.com

Then, in Open Code, run /agent and paste the token printed here.

Safety model (read this):
  * listens on 127.0.0.1 only, and only answers requests that carry the secret token
  * only the web sites you list with --origin may talk to it (browser CORS), and the
    Host header is checked against DNS-rebinding tricks
  * Open Code asks you to approve every command before it is sent here
  * commands start in the project folder, with a clean environment (variables whose
    names contain KEY, TOKEN, SECRET, PASSWORD... are removed), no stdin, a timeout and
    a cap on output; sudo and a few catastrophic patterns are refused
  * this is NOT a sandbox: an approved command can do whatever your user account can do.
    Read each command before you click Yes.
"""
import argparse
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

MAX_OUT = 30000
MAX_CMD = 4000
MAX_BODY = 16384
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300

SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)

# A safety net against obvious disasters, not a sandbox.
DENY = [
    (r"\bsudo\b|\bdoas\b|\bsu\s+-?\s*\w*\s*(-c|$)", "sudo/su is blocked"),
    (r"\brm\s+(?:-[-\w]+\s+)+(?:/|/\*|~|~/|\$HOME|\$\{HOME\}|\.\.)(?:\s|$)", "recursive delete of / ~ or .. is blocked"),
    (r"\bmkfs\b|\bdd\s+[^|;&]*\bof=/dev/|>\s*/dev/(?:sd|nvme|disk|hd)", "writing to disks is blocked"),
    (r":\(\)\s*\{", "fork bombs are blocked"),
    (r"\b(?:shutdown|reboot|halt|poweroff)\b", "shutdown/reboot is blocked"),
    (r"\bformat\s+[a-z]:|\bdel\s+/[sq]\b[^|;&]*[a-z]:\\", "formatting/wiping a drive is blocked"),
    (r"\b(?:curl|wget|iwr|invoke-webrequest)\b[^;&]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|python[\d.]*|pwsh|powershell|iex)\b",
     "piping a download into a shell is blocked"),
    (r"\bchmod\s+-R\s+0?777\s+/", "recursive chmod on / is blocked"),
]
DENY = [(re.compile(p, re.I), why) for p, why in DENY]

ROOT = Path(".")
TOKEN = ""
ORIGINS = set()
HOSTS = set()
RUN_LOCK = threading.Lock()
CURRENT = {"p": None}


def clean_env():
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env.update({"TERM": "dumb", "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat", "CI": "1",
                "GIT_TERMINAL_PROMPT": "0", "PYTHONUNBUFFERED": "1"})
    return env


def refused(cmd):
    for rx, why in DENY:
        if rx.search(cmd):
            return why
    return None


def kill(p):
    try:
        if os.name == "posix":
            os.killpg(p.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        try:
            p.kill()
        except OSError:
            pass


def shorten(text):
    if len(text) <= MAX_OUT:
        return text, False
    cut = len(text) - MAX_OUT
    return text[:20000] + f"\n…[{cut} characters cut]…\n" + text[-(MAX_OUT - 20000):], True


def run(cmd, timeout):
    kw = dict(shell=True, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=clean_env())
    if os.name == "posix":
        kw["start_new_session"] = True  # own process group, so a timeout can kill the whole tree
    p = subprocess.Popen(cmd, **kw)
    CURRENT["p"] = p
    timed_out = False
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill(p)
        try:
            out, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = b""
    finally:
        CURRENT["p"] = None
    text, truncated = shorten((out or b"").decode("utf-8", "replace"))
    code = p.returncode if p.returncode is not None else -1
    return {"code": code, "output": text, "timed_out": timed_out, "truncated": truncated}


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenCodeAgent"
    sys_version = ""
    timeout = 30

    def log_message(self, *a):
        pass

    # --- helpers
    def origin_ok(self):
        o = self.headers.get("Origin")
        return o is None or o in ORIGINS

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        o = self.headers.get("Origin")
        if o in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def err(self, code, msg):
        self.send_json(code, {"error": {"message": msg, "code": code}})

    def authed(self):
        got = self.headers.get("Authorization", "")
        return got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), TOKEN.encode())

    # --- routes
    def handle_any(self):
        if self.headers.get("Host", "").lower() not in HOSTS:
            return self.err(403, "Forbidden host")
        if not self.origin_ok():
            return self.err(403, "This website is not allowed. Start the agent with --origin <its address>.")
        path = urlparse(self.path).path
        if self.command == "OPTIONS":
            return self._preflight()
        if not self.authed():
            return self.err(401, "Missing or wrong token")
        if path == "/ping" and self.command == "GET":
            return self.send_json(200, {"ok": True, "name": ROOT.name or str(ROOT), "os": os.name})
        if path == "/cancel" and self.command == "POST":
            p = CURRENT["p"]
            if p:
                kill(p)
            return self.send_json(200, {"ok": True, "cancelled": bool(p)})
        if path == "/run" and self.command == "POST":
            return self.run_command()
        self.err(404, "Not found")

    def _preflight(self):
        self.send_response(204)
        o = self.headers.get("Origin")
        if o in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def run_command(self):
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.err(415, "Content-Type must be application/json")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.err(411, "Content-Length required")
        if n < 0 or n > MAX_BODY:
            return self.err(413, "Request too large")
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            return self.err(400, "Invalid JSON")
        cmd = body.get("command") if isinstance(body, dict) else None
        if not isinstance(cmd, str) or not cmd.strip():
            return self.err(400, "command is required")
        if len(cmd) > MAX_CMD:
            return self.err(400, f"command is longer than {MAX_CMD} characters")
        why = refused(cmd)
        if why:
            print(f"[{time.strftime('%H:%M:%S')}] REFUSED ({why}): {cmd}", flush=True)
            return self.err(403, "Command refused by the agent: " + why)
        try:
            timeout = min(max(int(body.get("timeout") or DEFAULT_TIMEOUT), 1), MAX_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        if not RUN_LOCK.acquire(blocking=False):
            return self.err(429, "Another command is still running")
        try:
            print(f"[{time.strftime('%H:%M:%S')}] RUN: {cmd}", flush=True)
            res = run(cmd, timeout)
            print(f"[{time.strftime('%H:%M:%S')}]   exit {res['code']}{' (timed out)' if res['timed_out'] else ''}", flush=True)
        finally:
            RUN_LOCK.release()
        self.send_json(200, res)

    def guarded(self):
        try:
            self.handle_any()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:
            sys.stderr.write(f"internal error: {type(e).__name__}\n")
            try:
                self.err(500, "Internal error")
            except OSError:
                pass

    do_GET = do_POST = do_OPTIONS = guarded


def main():
    global ROOT, TOKEN, ORIGINS, HOSTS
    ap = argparse.ArgumentParser(description="Open Code local agent")
    ap.add_argument("--dir", default=".", help="project folder the commands run in (default: current folder)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--origin", action="append", default=[], help="web address allowed to use the agent, e.g. https://my-app.onrender.com (repeatable)")
    args = ap.parse_args()

    ROOT = Path(args.dir).expanduser().resolve()
    if not ROOT.is_dir():
        sys.exit(f"Folder not found: {ROOT}")
    if ROOT == Path(ROOT.anchor) or ROOT == Path.home():
        sys.exit("Choose a project folder, not the drive root or your whole home folder.")
    TOKEN = os.environ.get("OPEN_CODE_AGENT_TOKEN", "").strip() or secrets.token_urlsafe(32)
    ORIGINS = {"http://localhost:8787", "http://127.0.0.1:8787"} | {o.rstrip("/").lower() for o in args.origin}
    HOSTS = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print("Open Code local agent")
    print(f"  folder        : {ROOT}")
    print(f"  listening on  : http://127.0.0.1:{args.port}  (this computer only)")
    print(f"  allowed sites : {', '.join(sorted(ORIGINS))}")
    print(f"  token         : {TOKEN}")
    print("\nIn Open Code run /agent and paste the token. Every command is logged below. Ctrl+C stops the agent.\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
