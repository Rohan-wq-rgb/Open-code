# Open Code

A private, self-hosted chat and coding assistant on top of [OpenRouter](https://openrouter.ai).
Your OpenRouter API key lives **on the server** (as an environment variable) and never reaches the browser.
You sign in with a password; chat history is stored and searchable on the server.

```
server.py        backend: sign-in, API-key proxy with rotation, history (SQLite), security headers
public/          the web app (index.html, app.js, style.css)
local-agent/     agent.py: runs the assistant's shell commands on YOUR computer (optional)
tests/           end-to-end tests (server, sandbox terminal, local agent)
render.yaml      one-click Render blueprint
```

No packages to install: the server uses only the Python standard library.

## Deploy on Render

1. **Push to GitHub** (keep the repo private if you like):
   ```bash
   git init && git add . && git commit -m "Open Code"
   git branch -M main
   git remote add origin https://github.com/<you>/open-code.git
   git push -u origin main
   ```
2. In Render: **New → Blueprint**, pick the repo. Render reads `render.yaml`.
3. When asked, fill in the two secrets:
   - `APP_PASSWORD`: your sign-in password, **12+ characters** (use a long passphrase).
   - `OPENROUTER_API_KEYS`: your key, or up to 15 keys separated by commas. When one is rate-limited, out of credits or invalid, the server switches to the next automatically.
4. Deploy, open your `https://<name>.onrender.com` URL and sign in.
5. Custom domain? Add it in Render, then set `ALLOWED_HOSTS=chat.example.com` (comma-separated for several).

**History persistence.** The blueprint uses the *Starter* plan with a 1 GB disk, so chats survive restarts and deploys.
To try it on the free plan instead, delete the `disk:` block and the `DATA_DIR` variable and set `plan: free`.
History then resets on every restart or deploy, and the service sleeps when idle.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `APP_PASSWORD` | yes (12+ chars in production) | sign-in password |
| `OPENROUTER_API_KEYS` or `OPENROUTER_API_KEY` | yes | one key or a comma-separated list (max 15) |
| `SESSION_SECRET` | yes (32+ chars in production) | signs session cookies; Render generates it |
| `DATA_DIR` | no | where `chats.db` is kept (default `./data`; `/var/data` on Render) |
| `ALLOW_PAID_MODELS` | no, default `false` | `true` lets the app use paid models |
| `ALLOWED_HOSTS` | no | extra host names (custom domains) |
| `SESSION_DAYS` | no, default `7` | how long a sign-in lasts |
| `MAX_TOKENS` | no, default `8192` | cap on tokens per reply |
| `CHAT_RATE_PER_MIN` | no, default `60` | chat requests per minute per client |
| `APP_URL` | no | sent to OpenRouter as the referrer (for their app listing) |

In production (`RENDER=true`, set by Render) the server **refuses to start** if the password, session secret or API key are missing or weak.

## Security

What is in place:

- **Your API key never leaves the server.** The browser only talks to this server; the key is not in the page, in responses, in logs or in error messages.
- **Everything is behind a password.** Only the static page and the sign-in call are public. Sessions are HMAC-signed cookies: `HttpOnly`, `Secure`, `SameSite=Strict`, `__Host-` prefixed, 7-day expiry. Changing `APP_PASSWORD` or `SESSION_SECRET` signs everyone out.
- **Brute-force protection.** 5 wrong passwords lock that client out for 15 minutes, and a global limit applies on top. Password comparison is constant-time.
- **Cost protection.** Paid models are blocked by default (only `openrouter/free` and `:free` models pass), replies are capped, and the request is rebuilt from an allow-list of fields so extra options (provider routing, plugins) cannot be injected. Also set a **credit limit on your OpenRouter key** as a second line of defence.
- **Cross-site protection.** No CORS. State-changing requests must come from the same origin and be `application/json`; unknown `Host` headers are rejected.
- **Browser hardening.** Strict Content-Security-Policy (no inline or eval'd scripts, no framing), HSTS, `nosniff`, no referrer, `noindex`. HTTP is redirected to HTTPS.
- **Input limits.** Body-size caps, validated chat ids, parameterised SQL, connection and concurrent-reply limits, request timeouts, and generic error messages with no stack traces.
- **Data.** Chats are stored in SQLite (`chats.db`, mode 600) and are never served as a file.

What to keep in mind:

- It is a **single-user app with one shared password**, not a multi-user service.
- Rate limits are in memory and per instance, so run one instance (the default).
- Use a **dedicated OpenRouter key** for this app, and rotate it if you ever think it leaked.
- Files you attach or let the assistant read are sent to the model. Keep **"confirm writes"** on when using `/folder`, since text inside a file could try to steer the model.
- `/folder` reads and writes files **on your own computer** through the browser (Chrome or Edge), and `/agent` runs commands there too.
- `/term` runs commands **on the server** in a sandbox (see above). It is not a guaranteed no-internet sandbox.

## Let the assistant run commands (local agent)

The assistant can run shell commands (install packages, run tests, linters, builds) and read the results.
Those commands run **on your own computer**, never on Render, through a small separate program, `local-agent/agent.py`.
Reason: the Render server holds your API key and password, and your project files are on your computer.

1. On your computer, open a terminal **inside your project folder** and start the agent (it needs only Python 3.9+):
   ```bash
   python /path/to/open-code-server/local-agent/agent.py --dir . --origin https://YOUR-APP.onrender.com
   ```
   It prints a one-time **token**. (`/agent` in the app shows this exact command with your address filled in.)
2. In Open Code run **`/agent`**, paste the token, and press Connect. The button turns orange.
3. Ask for something like "install the dependencies and run the tests". For every command the app shows a prompt:
   **Yes**, **Yes, don't ask again this session**, or **No**.

Use Chrome, Edge or Firefox (Safari blocks calls from an https site to your own computer). Chrome may ask to allow
local-network access the first time: choose Allow. The agent keeps a log of every command in its terminal window.

How it is protected:

- It listens on `127.0.0.1` only and answers only requests carrying the secret token (compared in constant time).
- Only the web addresses you pass with `--origin` can talk to it, and the Host header is checked against DNS-rebinding.
- **You approve every command** in the browser first.
- Commands start in your project folder with a cleaned environment (variables with KEY, TOKEN, SECRET, PASSWORD... in the
  name are removed), no keyboard input, a 60 s default timeout (max 300 s) and a 30 KB output limit.
  `sudo`, recursive deletes of `/` or `~`, disk wiping, shutdown and "download | sh" are refused.
- **It is not a sandbox.** An approved command can do anything your user account can. Read each command before clicking Yes,
  and start the agent in a project folder, not your home folder (the agent refuses to start there).
- Set `OPEN_CODE_AGENT_TOKEN` if you want the same token on every start.

## Sandbox terminal on the server (/term)

`/term` opens a terminal that runs **on the Render server**, so it works from Android Chrome too. The assistant can also use it
through the `run_command` tool when no local agent is connected.

- **One at a time.** Opening a terminal closes the previous one. This matters: all sessions run as the same user.
- **Temporary folder.** Each terminal gets its own folder under `TERM_DIR` (default: the system temp folder). On the free plan
  it is erased on restart, and it is deleted when you close the terminal or after an hour of inactivity.
- **Unprivileged user.** When the server runs as root (check the start-up log for `terminal: enabled, runs as user 'nobody'`),
  commands run as `nobody`. That stops them reading the server's chat database and process memory.
  **In production the terminal stays disabled unless the server can do this**, unless you set `TERM_ALLOW_SAME_USER=1`,
  which is not recommended.
- **No secrets.** Commands get a fresh environment. The OpenRouter key, password and session secret are not in it.
- **Limits.** 60 s per command (120 s max), 1.5 GB memory, 200 MB per file, 256 open files, 256 processes,
  30 KB output, 120 commands per minute.
- **One-shot commands only.** Long-running servers (`npm run dev`) are not supported. Background jobs are killed when a
  command times out.

**What is NOT guaranteed:**
- **Internet.** Render's free plan has no outbound firewall setting, so the terminal cannot reliably block the network. It
  only points common tools (`curl`, `pip`, `npm`, Python) at a closed proxy, and a program that opens sockets directly can
  still reach the internet. Treat the sandbox as able to reach the internet.
- **Isolation.** It is not a virtual machine. It is a separate unprivileged process with limits. Anyone who can sign in can run
  commands on the server, and a determined user could try to exploit the server itself. Keep the password private.
- **Node.js and other tools** may be missing from the Python runtime. `python3` and `sh` are normally there.

## Run locally

```bash
APP_PASSWORD="a long local passphrase" OPENROUTER_API_KEYS="sk-or-v1-..." python server.py
# open http://localhost:8787
```
Without `APP_PASSWORD` a random one is generated and printed at start-up.

## Tests

```bash
python -m unittest discover -s tests -v
```
They start the real server against a mock OpenRouter and check sign-in, lockout, cookie flags, key rotation, key secrecy, paid-model blocking, CSRF/host checks, history search and the production start-up rules. They also start the local agent and check its token, origin and host checks, secret scrubbing, refusals, timeouts, output limits and cancel. GitHub Actions runs them on every push.
