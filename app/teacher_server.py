#!/usr/bin/env python3
"""
Serveur de l'app 中文 · 写汉字 + « professeur » (API OpenAI-compatible).

    python3 teacher_server.py            # http://0.0.0.0:8099/
    python3 teacher_server.py --selftest # envoie un faux profil au modèle

- Sert app/index.html (statique, aucun CDN).
- POST /api/teacher       : receives a progress snapshot, returns a feedback JSON.
- POST /api/lesson-result : stores a finished lesson run (scores, answers) in results.jsonl.
- GET  /api/health        : service status (model, reasoning effort, key present).
- GET  /api/results       : last stored lesson runs.
The API key stays server-side: the browser never sees it.
"""
import base64
import difflib
import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import uuid
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = Path(__file__).resolve().parent
PROFILE = APP.parent
CONF_PATH = APP / "teacher.json"
LOG_PATH = APP / "teacher.log.jsonl"
BASE_PATH = "/chinois"            # préfixe public historique sur ymzindustry.tech
EXTRA_PREFIXES = ("/chinese",)    # alias demandé (le proxy doit router ce chemin vers ici)
RESULTS_PATH = APP / "results.jsonl"
SHARED_LOG = PROFILE / "shared-folder" / "logs.txt"   # journal partagé avec le profil webdeveloper
ASR_VENV = PROFILE / "runtime" / ".venv-asr" / "bin" / "python"   # hors de cache/ (purgé)
ASR_WORKER = APP / "asr_worker.py"
AUTH_PATH = APP / "auth.json"
SECRET_PATH = APP / ".session_secret"
ACCESS_LOG = APP / "access.log"

DEFAULTS = {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "reasoning_effort": "medium",   # low | medium | high ("" = ne pas l'envoyer)
    "temperature": 0.7,
    "max_tokens": 1100,
    "timeout_s": 90,
    "api_key_env": "DEEPSEEK_API_KEY",
    "min_seconds_between_calls": 4,
    "max_body_bytes": 65536,
}
ALLOWED_ORIGIN = None  # même origine uniquement


def load_config():
    cfg = dict(DEFAULTS)
    if CONF_PATH.exists():
        try:
            cfg.update(json.loads(CONF_PATH.read_text(encoding="utf-8")))
        except Exception as e:                     # config cassée : on garde les défauts
            print("[warn] teacher.json illisible:", e, file=sys.stderr)
    return cfg


def load_env():
    """Lit les variables du .env du profil sans écraser l'environnement."""
    env = {}
    p = PROFILE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
CFG = load_config()


def api_key():
    return os.environ.get(CFG["api_key_env"]) or ENV.get(CFG["api_key_env"]) or ""


SYSTEM_PROMPT = """You are 老师, a Mandarin teacher for adult English-speaking learners (this student is a French speaker who works in English).
He has about one year of Chinese (HSK1) and works in short blocks: new words one page at a time, then matching characters to their meaning, then filling the missing character in a spoken sentence, then tone and shadowing drills.

You receive: his KPIs, his weak words, the progress of the 5 lessons, his last 40 actions, and the lesson he just finished — its matching score (first try only), its fill-the-gap score, the exact tone items he got right or wrong, and his speaking self-ratings.

Absolute rules:
- Answer in English, address him as "you", stay direct and concrete. No vague praise.
- Any Chinese you produce must be short (max 10 characters) and certain. If you are not sure of a sentence, give a single word or a noun phrase. Never invent a sentence.
- Pinyin must carry tone marks (nǐ hǎo). Never write pinyin without tone marks.
- Never claim you checked his handwriting: he judges his own flashcards in the review tab. Comment on his results, not on his strokes.
- At most 3 weak points, 3 actions, 3 exercise items, taken from his weak words and from the tone items he just missed.
- No lecturing, no long lists.

Answer with a single json object of exactly this shape:
{"summary": "2-3 sentences", "weak_points": [{"item": "汉字 + pinyin", "why": "..."}],
 "plan": ["action 1", "action 2"], "exercise": {"instruction": "...",
 "items": [{"hanzi": "...", "pinyin": "...", "meaning": "..."}]}, "encouragement": "one sentence"}"""


def build_user_prompt(snap, concise=False):
    tail = ("\nBe shorter: summary in 3 sentences max, 2 weak points, "
            "2 actions, 2 exercise items. Return a complete, closed json object.") if concise else ""
    return (
        "Here is the student's data (snapshot). Analyse it and answer in json.\n"
        + json.dumps(snap, ensure_ascii=False, separators=(",", ":")) + tail
    )


def build_payload(snap, concise=False):
    payload = {
        "model": CFG["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(snap, concise)},
        ],
        "temperature": CFG["temperature"],
        "max_tokens": CFG["max_tokens"],
        "response_format": {"type": "json_object"},
    }
    if CFG.get("reasoning_effort"):
        payload["reasoning_effort"] = CFG["reasoning_effort"]
    return payload


def call_model(snap, concise=False):
    key = api_key()
    if not key:
        raise RuntimeError("clé API absente (%s)" % CFG["api_key_env"])
    payload = build_payload(snap, concise)

    url = CFG["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps(payload).encode("utf-8")

    def once(b):
        req = urllib.request.Request(url, data=b, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        })
        try:
            with urllib.request.urlopen(req, timeout=CFG["timeout_s"]) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:600]
            raise RuntimeError("API %s : %s" % (e.code, detail))

    t0 = time.time()
    try:
        resp = once(body)
    except RuntimeError as e:
        # certains endpoints refusent reasoning_effort : on réessaie sans
        if "reasoning" in str(e).lower() and "reasoning_effort" in payload:
            payload.pop("reasoning_effort", None)
            resp = once(json.dumps(payload).encode("utf-8"))
        else:
            raise
    ms = int((time.time() - t0) * 1000)
    text = resp["choices"][0]["message"]["content"]
    usage = resp.get("usage", {})
    return text, ms, usage


def parse_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def log_event(rec):
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass



# --------------------------------------------------------------------------
# Login gate. Credentials live in app/auth.json, either
#   {"user": "name", "pass": "plain text"}          (hashed in memory at load)
#   {"user": "name", "sha256": "<hex digest>"}      (preferred, no plaintext)
# No file -> no gate. Sessions are signed cookies, so no plaintext travels.
# --------------------------------------------------------------------------
def shared_log(msg):
    """Journal lisible par l'autre agent (shared-folder/logs.txt). Jamais de secret."""
    try:
        SHARED_LOG.parent.mkdir(parents=True, exist_ok=True)
        if SHARED_LOG.exists() and SHARED_LOG.stat().st_size:
            try:
                with SHARED_LOG.open("rb") as f:
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":
                        with SHARED_LOG.open("a", encoding="utf-8") as g:
                            g.write("\n")
            except Exception:
                pass
        if SHARED_LOG.exists() and SHARED_LOG.stat().st_size > 512_000:
            keep = SHARED_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-800:]
            SHARED_LOG.write_text("\n".join(keep) + "\n", encoding="utf-8")
        with SHARED_LOG.open("a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except Exception:
        pass


def auth_config():
    try:
        st = AUTH_PATH.stat()
        raw = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        print("[warn] auth.json illisible — accès ouvert", file=sys.stderr)
        return None
    user = str(raw.get("user") or raw.get("login") or "").strip()
    pwd = raw.get("pass") or raw.get("password")
    sha = str(raw.get("sha256") or "").lower()
    if not sha and pwd:
        sha = hashlib.sha256(str(pwd).encode("utf-8")).hexdigest()
    if not user or not sha:
        print("[warn] auth.json incomplet (user + pass/sha256) — accès ouvert", file=sys.stderr)
        return None
    return {"user": user, "sha": sha}


def session_secret():
    if SECRET_PATH.exists():
        s = SECRET_PATH.read_text(encoding="utf-8").strip()
        if s:
            return s
    s = secrets.token_hex(32)
    SECRET_PATH.write_text(s, encoding="utf-8")
    try:
        SECRET_PATH.chmod(0o600)
    except Exception:
        pass
    return s


SESSION_TTL = 12 * 3600          # one login per session, expires on its own


def _sig(cfg, iat):
    msg = ("%s|%s|%d" % (cfg["user"], cfg["sha"], iat)).encode("utf-8")
    return hmac.new(session_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:40]


def session_token(cfg, issued=None):
    iat = int(issued if issued is not None else time.time())
    return "%d.%s" % (iat, _sig(cfg, iat))


def session_valid(cfg, cookie):
    if not cfg:
        return True
    if not cookie or "." not in cookie:
        return False
    raw, sig = cookie.split(".", 1)
    try:
        iat = int(raw)
    except ValueError:
        return False
    if time.time() - iat > SESSION_TTL:
        return False
    return hmac.compare_digest(sig, _sig(cfg, iat))


STATE_DIR = APP / "state"


def user_state_path(cfg):
    tag = hashlib.sha256(cfg["user"].encode("utf-8")).hexdigest()[:16]
    return STATE_DIR / (tag + ".json")


def read_user_state(cfg):
    p = user_state_path(cfg)
    if not p.exists():
        return {"savedAt": 0, "state": None}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {"savedAt": int(d.get("savedAt") or 0), "state": d.get("state")}
    except Exception:
        return {"savedAt": 0, "state": None}


def write_user_state(cfg, saved_at, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = user_state_path(cfg)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"savedAt": int(saved_at), "state": state}, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(p)
    try:
        p.chmod(0o600)
    except Exception:
        pass


def parse_cookies(header):
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,maximum-scale=1">
<meta name="theme-color" content="#0b0e17">
<title>中文 · sign in</title><style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;margin:0}
body{position:fixed;inset:0;overflow:hidden;overscroll-behavior:none;
background:#0b0e17;color:#eef1ff;
font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
display:flex;align-items:center;justify-content:center;
padding:calc(env(safe-area-inset-top,0px) + 14px) 16px calc(env(safe-area-inset-bottom,0px) + 14px)}
.card{width:100%;max-width:360px;max-height:100%;overflow-y:auto;background:#151a29;
border:1px solid #28304a;border-radius:20px;padding:22px}
h1{font-size:20px;margin:0 0 2px}
.t{color:#96a0c0;font-size:11px;letter-spacing:1.5px;text-transform:uppercase}
label{display:block;font-size:12px;color:#96a0c0;margin:16px 0 6px}
input{width:100%;background:#1c2233;border:1px solid #28304a;border-radius:12px;
padding:13px 14px;color:#eef1ff;font:inherit;font-size:16px;outline:none}
input:focus{border-color:#3b4468}
button{width:100%;margin-top:20px;padding:14px;border:0;border-radius:12px;font:inherit;font-weight:650;
background:linear-gradient(135deg,#ff5a5f,#e2454f);color:#fff;cursor:pointer}
button:active{transform:scale(.99)}
.err{margin-top:14px;color:#ff9b9b;font-size:13px}
.back{display:block;text-align:center;margin-top:14px;color:#96a0c0;font-size:13px;text-decoration:none}
/* short screens and the on-screen keyboard: shrink instead of scrolling the page */
@media (max-height:600px){.card{padding:16px}h1{font-size:18px}label{margin:12px 0 5px}button{margin-top:14px}}
@media (max-height:460px){.t{display:none}h1{font-size:17px;margin-bottom:6px}label{font-size:11px;margin:9px 0 4px}
input{padding:10px 12px}button{padding:11px;margin-top:11px}}
</style></head><body><form class="card" method="post" action="__ACTION__"><input type="hidden" name="next" value="__NEXT__">
<div class="t">中文 · private</div><h1>Sign in</h1>
<label for="u">Username</label><input id="u" name="u" autocomplete="username" autocapitalize="none" spellcheck="false" required>
<label for="p">Password</label><input id="p" name="p" type="password" autocomplete="current-password" required>
<button type="submit">Enter</button>__ERR__
<a class="back" href="__HOME__">Continue as guest</a></form></body></html>"""


# --------------------------------------------------------------------------
# Local speech recognition (whisper small, int8, CPU). One persistent worker so
# the model is loaded once; ~2-4 s per short clip. Nothing leaves the machine.
# --------------------------------------------------------------------------
_ASR = {"proc": None, "lock": threading.Lock(), "ready": False}


def asr_available():
    return ASR_VENV.exists() and ASR_WORKER.exists()


def _asr_start():
    if _ASR["proc"] is not None and _ASR["proc"].poll() is None:
        return _ASR["proc"]
    _ASR["proc"] = subprocess.Popen(
        [str(ASR_VENV), str(ASR_WORKER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1)
    hello = _ASR["proc"].stdout.readline()          # {"ready": true}
    _ASR["ready"] = bool(hello)
    return _ASR["proc"]


def asr_text(path, lang="zh"):
    with _ASR["lock"]:
        if not asr_available():
            raise RuntimeError("pronunciation engine not installed on this server")
        proc = _asr_start()
        proc.stdin.write(json.dumps({"audio": str(path), "lang": lang}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("ASR worker arrêté")
        data = json.loads(line)
        if "error" in data:
            raise RuntimeError(data["error"])
        return data.get("text", "")


HAN = lambda c: "\u4e00" <= c <= "\u9fff"


def compare_pronunciation(target, heard):
    """Aligne les caractères entendus sur la phrase cible.

    Trois états, comme le veut l'élève :
      ok    (vert)  le bon caractère a été entendu
      weak  (gris)  un autre caractère à sa place : le son est là mais il est flou
      miss  (rouge) rien n'a été entendu pour ce caractère
    """
    idx = [i for i, c in enumerate(target) if HAN(c)]
    tgt = "".join(target[i] for i in idx)
    got = "".join(c for c in heard if HAN(c))
    sm = difflib.SequenceMatcher(None, tgt, got, autojunk=False)
    state = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        for k in range(i1, i2):
            state[idx[k]] = {"equal": "ok", "replace": "weak", "delete": "miss"}.get(tag, "miss")
    chars = [{"c": c, "s": state.get(i, "neutral")} for i, c in enumerate(target)]
    pts = sum(1.0 if x["s"] == "ok" else 0.5 if x["s"] == "weak" else 0.0 for x in chars if x["s"] != "neutral")
    return {"heard": heard, "score": round(pts / max(1, len(idx)) * 100), "chars": chars}



# --------------------------------------------------------------------------
# Short LLM advice for a pronunciation attempt (text only: the ASR transcript).
# Cached on (target, heard) so repeated checks cost nothing.
# --------------------------------------------------------------------------
_PRON_CACHE = {}
_PRON_SYSTEM = """You are a Mandarin pronunciation coach for an English-speaking learner.
You receive the sentence they had to read and what a speech recogniser heard.
Write at most two short sentences of practical advice in English, naming the syllables that went wrong.
Never claim to hear tones: the transcript has none. Any pinyin you write must carry tone marks.
No greeting, no preamble."""


def _post_json(payload, timeout):
    req = urllib.request.Request(CFG["base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + api_key()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def pronunciation_feedback(target, heard, score, chars):
    key = (target, heard)
    if key in _PRON_CACHE:
        return _PRON_CACHE[key]
    if not api_key():
        return ""
    missed = " ".join(c["c"] for c in chars if c["s"] in ("weak", "miss"))
    payload = {
        "model": CFG["model"],
        "messages": [
            {"role": "system", "content": _PRON_SYSTEM},
            {"role": "user", "content": "Sentence: %s\nRecogniser heard: %s\nScore: %d%%\nCharacters missed: %s\n"
                                        "Write the advice." % (target, heard or "(nothing)", score, missed or "(none)")},
        ],
        "temperature": 0.4,
        "max_tokens": 220,
    }
    eff = CFG.get("pron_reasoning_effort", "low")
    if eff:
        payload["reasoning_effort"] = eff
    try:
        resp = _post_json(payload, 30)
        text = resp["choices"][0]["message"]["content"].strip()
    except Exception:
        text = ""
    if len(_PRON_CACHE) > 200:
        _PRON_CACHE.clear()
    _PRON_CACHE[key] = text
    return text


MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".ico": "image/x-icon", ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "zh-teacher/1.0"
    last_call = {}  # ip -> timestamp

    def log_message(self, fmt, *args):  # silence le bruit par requête
        pass

    def log_request(self, code="-", size="-"):
        """Trace chaque requête : indispensable pour savoir si le téléphone atteint le serveur."""
        try:
            with ACCESS_LOG.open("a", encoding="utf-8") as f:
                f.write("%s %s %s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                        self.client_address[0], self.command, self.path, code))
            shared_log("%s %s %s%s -> %s" % (self.client_address[0], self.command,
                                             getattr(self, "prefix", ""), self.path, code))
        except Exception:
            pass

    # ---------- auth ----------
    login_fails = {}          # ip -> [count, first_ts]

    def authed(self, cfg):
        if not cfg:
            return True
        cookie = parse_cookies(self.headers.get("Cookie")).get("zh_s", "")
        return session_valid(cfg, cookie)

    def login_page(self, error="", code=200, next_url=""):
        body = LOGIN_HTML.replace("__ACTION__", (getattr(self, "prefix", "") or "") + "/login")
        body = body.replace("__NEXT__", html.escape(next_url or "", quote=True))
        body = body.replace("__HOME__", (getattr(self, "prefix", "") or "") + "/")
        body = body.replace("__ERR__", '<div class="err">%s</div>' % html.escape(error) if error else "")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_login(self, cfg):
        ip = self.client_address[0]
        now = time.time()
        state = self.login_fails.get(ip, [0, now])
        if now - state[1] > 600:
            state = [0, now]
        if state[0] >= 8:
            return self.login_page("Too many attempts. Wait 10 minutes.", 429)
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 8192)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "ignore"))
        except Exception:
            form = {}
        user = (form.get("u") or [""])[0]
        pwd = (form.get("p") or [""])[0]
        nxt = (form.get("next") or [""])[0]
        if not (nxt.startswith("/") and not nxt.startswith("//")):
            nxt = (self.prefix or "") + "/"
        ok_user = cfg and hmac.compare_digest(user.strip(), cfg["user"])
        ok_pass = cfg and hmac.compare_digest(hashlib.sha256(pwd.encode("utf-8")).hexdigest(), cfg["sha"])
        if ok_user and ok_pass:
            self.login_fails.pop(ip, None)
            token = session_token(cfg)
            secure = "; Secure" if (self.headers.get("X-Forwarded-Proto", "").lower() == "https") else ""
            self.send_response(302)
            self.send_header("Set-Cookie", "zh_s=%s; Path=/; HttpOnly; SameSite=Lax%s" % (token, secure))
            self.send_header("Location", nxt)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        state[0] += 1
        self.login_fails[ip] = state
        return self.login_page("Wrong username or password.", 401)

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if path.suffix == ".html" else "max-age=300")
        self.end_headers()
        self.wfile.write(data)

    def resolve(self, url_path):
        rel = url_path.split("?")[0].lstrip("/") or "index.html"
        target = (APP / rel).resolve()
        if not str(target).startswith(str(APP.resolve())) or not target.is_file():
            return None
        return target

    # ---------- routes ----------
    def strip_base(self):
        """Retire le préfixe public : /chinois/api/x ou /chinese/api/x -> /api/x.

        Le site est servi à la racine sur le LAN et sous un préfixe derrière le
        proxy de ymzindustry.tech : les deux doivent fonctionner.
        """
        self.prefix = ""
        for base in ((BASE_PATH,) + EXTRA_PREFIXES):
            if base and (self.path == base or self.path.startswith(base + "/")):
                self.prefix = base
                self.path = self.path[len(base):] or "/"
                return

    def do_GET(self):
        self.strip_base()
        cfg = auth_config()
        path = self.path.split("?")[0]
        query = urllib.parse.parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
        next_url = (query.get("next") or [""])[0]

        if path == "/login":
            if cfg and self.authed(cfg):
                return self.redirect(next_url if next_url.startswith("/") and not next_url.startswith("//")
                                     else (self.prefix or "") + "/")
            return self.login_page(next_url=next_url)
        if path == "/logout":
            self.send_response(302)
            self.send_header("Set-Cookie", "zh_s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.send_header("Location", (self.prefix or "") + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/whoami":
            return self.send_json({"authed": bool(cfg) and self.authed(cfg), "gate": bool(cfg)})
        if cfg and not self.authed(cfg) and path.startswith("/api/") and path != "/api/health":
            return self.send_json({"error": "authentication required"}, 401)
        if self.path.startswith("/api/health"):
            return self.send_json({
                "ok": bool(api_key()),
                "model": CFG["model"],
                "reasoning_effort": CFG.get("reasoning_effort") or None,
                "pron_reasoning_effort": CFG.get("pron_reasoning_effort") or None,
                "base_url": CFG["base_url"],
                "key_env": CFG["api_key_env"],
                "asr": asr_available(),
            })
        if path == "/api/state":
            return self.send_json(read_user_state(cfg))
        if self.path.startswith("/api/results"):
            try:
                rows = [json.loads(l) for l in RESULTS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
            except Exception:
                rows = []
            return self.send_json({"count": len(rows), "results": rows[-40:]})
        if self.path.split("?")[0] in ("/", ""):
            f = APP / "index.html"
            return self.send_file(f) if f.exists() else self.send_json({"error": "index.html manquant"}, 404)
        f = self.resolve(self.path)
        if not f:
            return self.send_json({"error": "not found"}, 404)
        return self.send_file(f)

    def do_POST(self):
        self.strip_base()
        cfg = auth_config()
        if self.path.split("?")[0] == "/login":
            return self.handle_login(cfg)
        if cfg and not self.authed(cfg):
            return self.send_json({"error": "authentication required"}, 401)
        if self.path.startswith("/api/pronunciation"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 12_000_000:
                    raise ValueError("audio trop volumineux")
                payload = json.loads(self.rfile.read(n).decode("utf-8"))
                target = str(payload.get("target") or "")[:200]
                audio = base64.b64decode(payload.get("audio") or "")
                if not target or len(audio) < 400:
                    raise ValueError("audio ou phrase manquant")
                tmp = PROFILE / "cache" / ("asr-%s.webm" % uuid.uuid4().hex)
                tmp.write_bytes(audio)
                try:
                    heard = asr_text(tmp)
                finally:
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                res = compare_pronunciation(target, heard)
                fb = pronunciation_feedback(target, heard, res["score"], res["chars"])
                if fb:
                    res["feedback"] = fb
                shared_log("pronunciation %d%% heard=%r" % (res["score"], res["heard"][:40]))
                return self.send_json(res)
            except Exception as e:
                return self.send_json({"error": str(e)[:200]}, 500)
        if self.path.split("?")[0] == "/api/state":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 4_000_000:
                    raise ValueError("state too large")
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                state = body.get("state")
                if not isinstance(state, dict):
                    raise ValueError("state missing")
                saved_at = int(body.get("savedAt") or time.time() * 1000)
                current = read_user_state(cfg)
                if current["savedAt"] > saved_at:
                    return self.send_json({"ok": False, "savedAt": current["savedAt"],
                                           "state": current["state"]}, 409)
                write_user_state(cfg, saved_at, state)
                return self.send_json({"ok": True, "savedAt": saved_at})
            except Exception as e:
                return self.send_json({"error": str(e)[:200]}, 400)
        if self.path.startswith("/api/lesson-result"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > CFG["max_body_bytes"]:
                    raise ValueError("bad body")
                run = json.loads(self.rfile.read(n).decode("utf-8"))
                if not isinstance(run, dict) or "lessonId" not in run:
                    raise ValueError("bad run")
                rec = {"t": int(time.time()), "ip": self.client_address[0], "run": run}
                with RESULTS_PATH.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                return self.send_json({"ok": True})
            except Exception as e:
                return self.send_json({"error": str(e)[:200]}, 400)
        if not self.path.startswith("/api/teacher"):
            return self.send_json({"error": "not found"}, 404)
        ip = self.client_address[0]
        now = time.time()
        if now - self.last_call.get(ip, 0) < CFG["min_seconds_between_calls"]:
            return self.send_json({"error": "trop de requêtes, attends quelques secondes"}, 429)
        self.last_call[ip] = now

        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > CFG["max_body_bytes"]:
                raise ValueError("corps de requête invalide")
            snap = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(snap, dict):
                raise ValueError("instantané invalide")
        except Exception as e:
            return self.send_json({"error": "requête invalide: %s" % e}, 400)

        try:
            text, ms, usage = call_model(snap)
            try:
                data = parse_json(text)
            except Exception:                      # réponse tronquée : on redemande plus court
                text, ms2, usage2 = call_model(snap, concise=True)
                ms += ms2
                usage = usage2
                data = parse_json(text)
            if not isinstance(data, dict) or "summary" not in data:
                raise RuntimeError("réponse du modèle inexploitable")
            data.setdefault("weak_points", [])
            data.setdefault("plan", [])
            data.setdefault("encouragement", "")
            shared_log("teacher ok %dms tokens=%s lesson=%s" % (ms, usage.get("total_tokens"),
                       (snap.get("lesson_run") or {}).get("lesson")))
            log_event({"t": int(time.time()), "ms": ms, "model": CFG["model"],
                       "effort": CFG.get("reasoning_effort"), "usage": usage,
                       "ok": True, "weak": [w.get("hanzi") for w in snap.get("weak_words", [])][:5]})
            return self.send_json(data)
        except Exception as e:
            shared_log("teacher FAIL %s" % str(e)[:200])
            log_event({"t": int(time.time()), "ok": False, "error": str(e)[:300]})
            return self.send_json({"error": str(e)[:300]}, 502)


def selftest():
    snap = {
        "eleve": {"profil": "francophone, ~1 an de chinois, bases HSK1", "app": "flashcards écriture + tons"},
        "date": time.strftime("%Y-%m-%d"),
        "objectif_quotidien": 20,
        "kpis": {"cartes_aujourdhui": 12, "cartes_7j": 96, "serie_jours": 4,
                 "mots_rencontres": 21, "mots_maitrises": 6, "precision_ecriture_pct": 74,
                 "precision_tons_pct": 58, "repetition_orale_pct": 61,
                 "temps_moyen_par_carte_s": 7.4},
        "mots_faibles": [
            {"hanzi": "贵", "pinyin": "guì", "sens": "cher", "boite": 0, "justes": 1, "rates": 3},
            {"hanzi": "碗", "pinyin": "wǎn", "sens": "bol", "boite": 1, "justes": 1, "rates": 2},
            {"hanzi": "便宜", "pinyin": "piányi", "sens": "bon marché", "boite": 1, "justes": 2, "rates": 1},
        ],
        "lecons": [{"n": 1, "titre": "认识一下", "progression": 100, "maitrise": 72},
                   {"n": 2, "titre": "数字和钱", "progression": 66, "maitrise": 41}],
        "activite_recente": [{"type": "card", "mot": "贵", "resultat": "wrong", "il_y_a_min": 3},
                             {"type": "tone", "mot": "十", "resultat": "wrong", "il_y_a_min": 6}],
        "question": None,
    }
    print("→ appel du modèle", CFG["model"], "· effort:", CFG.get("reasoning_effort"),
          "· prononciation:", CFG.get("pron_reasoning_effort", "low"))
    text, ms, usage = call_model(snap)
    print("← %d ms · tokens %s" % (ms, usage))
    print(text[:2000])


def set_auth(user=None):
    """Écrit app/auth.json sans jamais afficher ni journaliser le mot de passe."""
    user = user or input("Username: ").strip()
    if not user:
        print("utilisateur vide — abandon")
        return 1
    pwd = getpass.getpass("Password (typed, not echoed): ")
    if len(pwd) < 6:
        print("mot de passe trop court (6 caractères minimum)")
        return 1
    if getpass.getpass("Repeat: ") != pwd:
        print("les deux saisies diffèrent")
        return 1
    AUTH_PATH.write_text(json.dumps({"user": user,
        "sha256": hashlib.sha256(pwd.encode("utf-8")).hexdigest()}, indent=2) + "\n", encoding="utf-8")
    try:
        AUTH_PATH.chmod(0o600)
    except Exception:
        pass
    print("auth.json écrit (mot de passe haché, jamais stocké en clair).")
    print("Redémarre l'app : ./start.sh   puis ouvre /login")
    return 0


def main():
    port, bind = 8099, "0.0.0.0"
    args = sys.argv[1:]
    if "--set-auth" in args:
        i = args.index("--set-auth")
        return set_auth(args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else None)
    if "--selftest" in args:
        return selftest()
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        if a == "--bind" and i + 1 < len(args):
            bind = args[i + 1]
        if a == "--base" and i + 1 < len(args):
            global BASE_PATH
            BASE_PATH = args[i + 1].rstrip("/")
    srv = ThreadingHTTPServer((bind, port), Handler)
    shared_log("--- start %s : port %d, prefixes %s, auth %s ---" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), port, "/".join((BASE_PATH,) + EXTRA_PREFIXES),
        "on" if auth_config() else "off"))
    print("中文 · 写汉字  →  http://%s:%d%s/  (modèle %s, effort %s, clé %s)"
          % (bind, port, BASE_PATH, CFG["model"], CFG.get("reasoning_effort") or "—",
             "ok" if api_key() else "MANQUANTE"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
