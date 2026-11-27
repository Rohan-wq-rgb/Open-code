"""Tests for the sandbox terminal API (server started in local-dev mode, as a subprocess)."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "correct horse battery staple"
SECRET = "s" * 40
API_KEY = "sk-or-test-AAAA"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipIf(os.name != "posix", "the terminal is Unix-only")
class TerminalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        env = {**os.environ, "PORT": str(cls.port), "HOST": "127.0.0.1", "DATA_DIR": cls.tmp.name + "/data",
               "TERM_DIR": cls.tmp.name + "/ws", "APP_PASSWORD": PASSWORD, "SESSION_SECRET": SECRET,
               "OPENROUTER_API_KEYS": API_KEY}
        for k in ("RENDER", "ENV"):
            env.pop(k, None)
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        c.request("POST", "/api/login", json.dumps({"password": PASSWORD}),
                  {"Host": f"localhost:{cls.port}", "Origin": f"http://localhost:{cls.port}", "Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        cls.cookie = r.getheader("Set-Cookie").split(";")[0]
        c.close()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def call(self, path, body=None, cookie=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}", "Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = self.cookie
        c.request("POST", path, json.dumps(body or {}), h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw else {})

    def open(self):
        st, j = self.call("/api/term/open")
        self.assertEqual(st, 200)
        return j["sid"]

    def term(self, sid, cmd, timeout=None):
        body = {"sid": sid, "command": cmd}
        if timeout:
            body["timeout"] = timeout
        return self.call("/api/term/run", body)

    def test_01_requires_sign_in_and_session_flag(self):
        self.assertEqual(self.call("/api/term/open", cookie=False)[0], 401)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/session", headers={"Host": f"localhost:{self.port}", "Cookie": self.cookie})
        self.assertTrue(json.loads(c.getresponse().read())["term"])

    def test_02_runs_in_its_own_folder(self):
        sid = self.open()
        st, j = self.term(sid, "echo hi; pwd")
        self.assertEqual(st, 200)
        self.assertEqual(j["code"], 0)
        self.assertIn("hi", j["output"])
        self.assertIn(sid, j["output"])  # the working folder is the session folder

    def test_03_secrets_are_not_visible(self):
        sid = self.open()
        _, j = self.term(sid, "env")
        out = j["output"]
        for secret in (API_KEY, SECRET, PASSWORD, "APP_PASSWORD", "OPENROUTER", "SESSION_SECRET"):
            self.assertNotIn(secret, out)

    def test_04_timeout_kills_the_command(self):
        sid = self.open()
        t = time.time()
        _, j = self.term(sid, "sleep 30", timeout=1)
        self.assertTrue(j["timed_out"])
        self.assertLess(time.time() - t, 10)

    def test_05_background_job_does_not_hang_the_server(self):
        sid = self.open()
        t = time.time()
        st, j = self.term(sid, "sleep 100 &", timeout=2)
        self.assertEqual(st, 200)
        self.assertLess(time.time() - t, 12)
        self.assertEqual(self.term(sid, "echo still-alive")[1]["output"].strip(), "still-alive")

    def test_06_output_is_capped(self):
        sid = self.open()
        _, j = self.term(sid, "head -c 100000 /dev/zero | tr '\\0' x")
        self.assertTrue(j["truncated"])
        self.assertLess(len(j["output"]), 40000)

    def test_07_one_terminal_at_a_time(self):
        first = self.open()
        second = self.open()
        self.assertEqual(self.term(first, "echo old")[0], 404)   # the older session was closed
        self.assertEqual(self.term(second, "echo new")[0], 200)

    def test_08_bad_and_unknown_sessions(self):
        self.assertEqual(self.term("bad id!!", "echo x")[0], 400)
        self.assertEqual(self.term("abcdefghijkl", "echo x")[0], 404)
        self.assertEqual(self.call("/api/term/run", {"sid": "abcdefghijkl", "command": "  "})[0], 400)

    def test_09_close_removes_the_session(self):
        sid = self.open()
        self.assertEqual(self.call("/api/term/close", {"sid": sid})[0], 200)
        self.assertEqual(self.term(sid, "echo x")[0], 404)

    def test_10_sandbox_user_when_root(self):
        if os.geteuid() != 0:
            self.skipTest("only checked when the server runs as root")
        sid = self.open()
        _, j = self.term(sid, "id -u")
        self.assertNotEqual(j["output"].strip(), "0")


if __name__ == "__main__":
    unittest.main()
