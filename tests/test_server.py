"""End-to-end tests: starts the real server as a subprocess against a mock OpenRouter."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "correct horse battery staple"
KEY_A, KEY_B = "sk-or-test-AAAA", "sk-or-test-BBBB"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream(BaseHTTPRequestHandler):
    seen = []          # keys that reached the "OpenRouter" mock
    limited = {KEY_A}  # keys answered with 429

    def log_message(self, *a):
        pass

    def do_POST(self):
        key = self.headers.get("Authorization", "").replace("Bearer ", "")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Upstream.seen.append((key, body))
        if key in Upstream.limited:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"rate limited"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for part in ("Hel", "lo"):
            self.wfile.write(('data: {"model":"mock/free","choices":[{"delta":{"content":"%s"}}]}\n\n' % part).encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up_port, cls.port = free_port(), free_port()
        cls.up = HTTPServer(("127.0.0.1", cls.up_port), Upstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        env = {**os.environ, "PORT": str(cls.port), "HOST": "127.0.0.1", "DATA_DIR": cls.tmp.name,
               "APP_PASSWORD": PASSWORD, "SESSION_SECRET": "s" * 40, "OPENROUTER_API_KEYS": f"{KEY_A},{KEY_B}",
               "ALLOW_TEST_UPSTREAM": "1", "OPENROUTER_BASE_URL": f"http://127.0.0.1:{cls.up_port}/api/v1"}
        for k in ("RENDER", "ENV", "ALLOW_PAID_MODELS"):
            env.pop(k, None)
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.up.shutdown()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, cookie=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"localhost:{self.port}", **(headers or {})}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
            if method != "GET":
                h.setdefault("Origin", f"http://localhost:{self.port}")
        if cookie:
            h["Cookie"] = cookie
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, r, raw

    def login(self):
        st, r, _ = self.req("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(st, 200)
        return r.getheader("Set-Cookie").split(";")[0]

    # ------------------------------------------------------------------ access control
    def test_static_is_public_but_api_is_not(self):
        self.assertEqual(self.req("GET", "/")[0], 200)
        self.assertEqual(self.req("GET", "/app.js")[0], 200)
        for path in ("/api/chats", "/api/chats/abc"):
            self.assertEqual(self.req("GET", path)[0], 401)
        self.assertEqual(self.req("POST", "/api/chat", {"model": "openrouter/free", "messages": [{"role": "user", "content": "x"}]})[0], 401)

    def test_wrong_password_and_lockout(self):
        # use a distinct client identity so this does not lock the other tests out
        for _ in range(5):
            st, _, _ = self.req("POST", "/api/login", {"password": "nope"}, headers={"X-Forwarded-For": "9.9.9.9"})
            self.assertEqual(st, 401)
        st, r, _ = self.req("POST", "/api/login", {"password": PASSWORD}, headers={"X-Forwarded-For": "9.9.9.9"})
        # dev mode does not trust X-Forwarded-For, so this is the same local client: locked out
        self.assertEqual(st, 429)
        self.assertIsNotNone(r.getheader("Retry-After"))

    def test_cookie_flags_and_session(self):
        Upstream.seen.clear()
        cookie = self.login()
        st, r, raw = self.req("GET", "/api/session", cookie=cookie)
        self.assertTrue(json.loads(raw)["authed"])
        # log in again to inspect the Set-Cookie header attributes
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("POST", "/api/login", json.dumps({"password": PASSWORD}),
                  {"Host": f"localhost:{self.port}", "Content-Type": "application/json", "Origin": f"http://localhost:{self.port}"})
        sc = c.getresponse().getheader("Set-Cookie")
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Strict", sc)
        self.assertEqual(self.req("GET", "/api/session", cookie="oc=garbage.value")[0], 200)
        self.assertFalse(json.loads(self.req("GET", "/api/session", cookie="oc=garbage.value")[2])["authed"])
        tampered = cookie[:-3] + ("AAA" if not cookie.endswith("AAA") else "BBB")
        self.assertEqual(self.req("GET", "/api/chats", cookie=tampered)[0], 401)

    # ------------------------------------------------------------------ request hygiene
    def test_security_headers(self):
        _, r, _ = self.req("GET", "/")
        for h in ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
            self.assertIsNotNone(r.getheader(h), h)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("Access-Control-Allow-Origin", dict(r.getheaders()))

    def test_host_origin_and_content_type_checks(self):
        cookie = self.login()
        self.assertEqual(self.req("GET", "/api/chats", cookie=cookie, host="evil.example")[0], 403)
        st, _, _ = self.req("DELETE", "/api/chats/abc", cookie=cookie, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("PUT", "/api/chats/abc", b"{}", cookie=cookie, headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)
        self.assertEqual(self.req("OPTIONS", "/api/chat")[0], 405)

    def test_limits_and_bad_input(self):
        cookie = self.login()
        self.assertEqual(self.req("PUT", "/api/chats/abc", b"{not json", cookie=cookie)[0], 400)
        self.assertEqual(self.req("PUT", "/api/chats/bad%20id!", {"msgs": []}, cookie=cookie)[0], 400)
        self.assertEqual(self.req("GET", "/api/chats/../../etc/passwd", cookie=cookie)[0], 400)  # rejected id
        self.assertEqual(self.req("GET", "/chats.db")[0], 404)
        self.assertEqual(self.req("GET", "/server.py")[0], 404)

    # ------------------------------------------------------------------ chat proxy
    def chat(self, cookie, model="openrouter/free"):
        return self.req("POST", "/api/chat", {"model": model, "stream": True, "messages": [{"role": "user", "content": "hi"}]}, cookie=cookie)

    def test_00_proxy_rotates_keys_and_never_leaks_them(self):
        # named 00 so it runs first: key A must still be un-parked when this starts
        cookie = self.login()
        Upstream.seen.clear()
        st, r, raw = self.chat(cookie)
        self.assertEqual(st, 200)
        self.assertIn(b"Hel", raw)
        used = [k for k, _ in Upstream.seen]
        self.assertEqual(used[:2], [KEY_A, KEY_B])           # A was rate-limited, B took over
        self.assertNotIn(KEY_A.encode(), raw)
        self.assertNotIn(KEY_B.encode(), raw)
        self.assertNotIn("sk-or", str(r.getheaders()))
        Upstream.seen.clear()
        self.chat(cookie)                                     # A is parked, B is used directly
        self.assertEqual([k for k, _ in Upstream.seen], [KEY_B])

    def test_paid_models_blocked_and_fields_filtered(self):
        cookie = self.login()
        st, _, raw = self.chat(cookie, model="anthropic/claude-opus-4")
        self.assertEqual(st, 403)
        self.assertIn(b"Paid models", raw)
        Upstream.seen.clear()
        self.req("POST", "/api/chat", {"model": "x/y:free", "messages": [{"role": "user", "content": "hi"}],
                                       "provider": {"order": ["evil"]}, "plugins": [{"id": "web"}], "max_tokens": 10**9}, cookie=cookie)
        sent = Upstream.seen[-1][1]
        self.assertNotIn("provider", sent)
        self.assertNotIn("plugins", sent)
        self.assertLessEqual(sent["max_tokens"], 8192)

    # ------------------------------------------------------------------ history
    def test_history_crud_and_search(self):
        cookie = self.login()
        chat = {"title": "Sort a list", "ts": 1, "msgs": [{"role": "user", "content": "How to sort in Python?"},
                                                         {"role": "assistant", "content": "Use sorted()."},
                                                         {"role": "tool", "tool_call_id": "t", "content": "secrettool"}]}
        self.assertEqual(self.req("PUT", "/api/chats/c1", chat, cookie=cookie)[0], 200)
        _, _, raw = self.req("GET", "/api/chats?q=python+sort", cookie=cookie)
        self.assertEqual([i["id"] for i in json.loads(raw)["items"]], ["c1"])
        _, _, raw = self.req("GET", "/api/chats?q=secrettool", cookie=cookie)
        self.assertEqual(json.loads(raw)["items"], [])
        _, _, raw = self.req("GET", "/api/chats?q=%25", cookie=cookie)       # a literal % must not match everything
        self.assertEqual(json.loads(raw)["items"], [])
        self.assertEqual(len(json.loads(self.req("GET", "/api/chats/c1", cookie=cookie)[2])["msgs"]), 3)
        self.assertEqual(self.req("DELETE", "/api/chats/c1", cookie=cookie)[0], 200)
        self.assertEqual(self.req("GET", "/api/chats/c1", cookie=cookie)[0], 404)


class ProductionModeTest(unittest.TestCase):
    """RENDER=true switches on the production rules."""

    def run_server(self, env_extra, wait=True):
        port = free_port()
        env = {k: v for k, v in os.environ.items() if k not in ("RENDER", "ENV")}
        env.update({"RENDER": "true", "PORT": str(port), "HOST": "127.0.0.1", **env_extra})
        proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if wait:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
        return proc, port

    def test_refuses_to_start_with_weak_or_missing_config(self):
        proc, _ = self.run_server({"APP_PASSWORD": "short", "SESSION_SECRET": "tooshort"}, wait=False)
        err = proc.communicate(timeout=10)[1].decode()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("APP_PASSWORD", err)
        self.assertIn("SESSION_SECRET", err)
        self.assertIn("OPENROUTER_API_KEY", err)

    def test_production_headers_cookie_and_redirect(self):
        with tempfile.TemporaryDirectory() as d:
            proc, port = self.run_server({"APP_PASSWORD": PASSWORD, "SESSION_SECRET": "z" * 40, "OPENROUTER_API_KEY": KEY_A,
                                          "RENDER_EXTERNAL_HOSTNAME": "app.onrender.com", "DATA_DIR": d})
            try:
                def go(method, path, host="app.onrender.com", headers=None, body=None):
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    c.request(method, path, body=body, headers={"Host": host, **(headers or {})})
                    r = c.getresponse()
                    r.read()
                    return r
                self.assertEqual(go("GET", "/healthz", host="10.0.0.5").status, 200)       # health check works on any host
                self.assertEqual(go("GET", "/", host="evil.example").status, 403)
                r = go("GET", "/", headers={"X-Forwarded-Proto": "http"})
                self.assertEqual((r.status, r.getheader("Location")), (308, "https://app.onrender.com/"))
                r = go("POST", "/api/login", headers={"X-Forwarded-Proto": "https", "Origin": "https://app.onrender.com",
                                                      "Content-Type": "application/json"}, body=json.dumps({"password": PASSWORD}))
                cookie = r.getheader("Set-Cookie")
                self.assertTrue(cookie.startswith("__Host-oc="), cookie)
                for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
                    self.assertIn(flag, cookie)
                self.assertIn("max-age=31536000", r.getheader("Strict-Transport-Security"))
            finally:
                proc.terminate()
                proc.wait(5)


if __name__ == "__main__":
    unittest.main()
