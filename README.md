# 中文 · 写汉字 — a Chinese trainer with a teacher on the other end

A single-file, offline-capable web app for learning Mandarin (HSK1 level), plus a small
Python server that gives it two things a static page cannot: **local speech recognition** for
pronunciation checks and **an LLM teacher** that reads each finished lesson and writes a note
about it.

Built for one learner (French native, ~1 year of Chinese). Interface in English.

![no dependencies](https://img.shields.io/badge/frontend-none-1f2937) ![stdlib server](https://img.shields.io/badge/server-Python%20stdlib-1f2937) ![offline](https://img.shields.io/badge/offline-first-1f2937)

---

## What it does

**16 micro-lessons** (10–15 slides each), each with its own focus:

| Focus | Shape of the lesson |
|---|---|
| Vocabulary | 5–6 new words, one page each → *Use them* → match → fill the gaps → word order → speaking |
| Grammar | 5–6 words → match → *Use them* → 2 grammar cards with examples → grammar drills → dialogue |
| Pronunciation | 5–6 words → match → *Use them* → tone drill → shadowing (3 → 4 → 6+ characters) → production |

Rules baked into the content:

- **One new point per screen.** Never a wall of vocabulary.
- **Nothing unseen in an exercise.** Every gap, ordering, dialogue and grammar sentence is filtered
  against the characters taught *up to that lesson* (plus a small set of particles 吗 了 的 呢 吧 是…).
- The 40 HSK1 base words are assumed known; everything else must appear in a lesson first.

Two flashcard decks, both using the same physical-pile logic:

- **Characters** — French prompt, you write the character with your finger, flip, then the back shows
  the **stroke order**, animated stroke by stroke (data embedded, no network), pinyin, example.
- **Sentences** — pinyin prompt (English optional), one slot per character, **9 characters in a 3 × 3
  keypad**, validate at the end; your row is coloured green/red above the correction.

Pile rules for both decks: *right* → bottom of the deck, *almost* → top of the deck, *missed* →
bottom of today's pile, so it comes back before the session ends. Session size is adjustable (5–60)
and can be re-cut on demand.

Other pieces: pronunciation check (record → local Whisper → per-character green/grey/red + a short
coach note), KPI dashboard, lesson history, and a teacher note attached to every finished lesson.

## Architecture

```
app/
  index.html        the whole frontend: HTML + CSS + JS, no build step, no CDN, no dependency
  teacher_server.py stdlib HTTP server: static files, login, JSON API, state sync
  asr_worker.py     persistent faster-whisper worker (stdin/stdout JSON line protocol)
  start.sh          python3 teacher_server.py --port 8099 --bind 0.0.0.0
  teacher.json      model + effort configuration (no key inside)
  showcase/         two static pages used for screenshots
```

**Frontend.** One file. State lives in `localStorage`; when you are logged in it is also pushed to
`/api/state` (debounced) and pulled at boot, newest `savedAt` wins. Audio for words uses the phone's
own `speechSynthesis` (zh-CN) — the paid API is never used for playback.

**Server.** `http.server` subclass, no framework:

| Route | Purpose |
|---|---|
| `GET /` | the app |
| `GET/POST /login`, `/logout` | single-account gate, signed session cookie |
| `GET /api/whoami` | am I logged in? |
| `GET /api/health` | model, reasoning effort, ASR availability |
| `GET/POST /api/state` | per-account progress sync (the whole state blob) |
| `POST /api/teacher` | lesson snapshot → feedback JSON (`summary`, `weak_points`, `plan`, `exercise`) |
| `POST /api/lesson-result` | append a finished run to `results.jsonl` |
| `POST /api/pronunciation` | audio → local Whisper → per-character verdict + advice |

**Two model calls, two budgets.** The end-of-lesson teacher runs at `medium` reasoning; the
pronunciation advice at `low` (it only reads a transcript). Both are set in `teacher.json`.
Pronunciation itself is **local** (faster-whisper, int8, CPU) — no audio ever leaves the machine.

## Run it

```bash
cd app
export DEEPSEEK_API_KEY=...     # or any OpenAI-compatible key
python3 teacher_server.py --port 8099 --bind 0.0.0.0
```

Then open `http://<host>:8099/`.

Without a key everything still works except the teacher: it falls back to a local rule-based
analysis. Without an account file the login gate is disabled.

### Optional pieces

**Accounts** (recommended if the app is reachable from the internet):

```bash
python3 teacher_server.py --set-auth          # asks for a username and a password
```

Writes `app/auth.json` with a SHA-256 hash (mode 600) and a random `.session_secret`. Same file also
serves as the API key gate: `/api/*` answers 401 without the session cookie, while the static app
stays public — visitors get a fully working demo with **zero** model calls.

**Pronunciation** (local whisper):

```bash
uv venv runtime/.venv-asr --python 3.12
uv pip install --python runtime/.venv-asr/bin/python faster-whisper
```

`teacher_server.py` looks for `runtime/.venv-asr/bin/python` next to `app/`. The first
transcription downloads the `small` model (~460 MB, cached under `~/.cache/huggingface`); after that
a check takes 2–4 s. `/api/health` reports `"asr": true/false`.

Edge case worth knowing: `av >= 15` removed the `metadata_errors` kwarg that faster-whisper still
passes, so `asr_worker.py` shims `av.open` and loads the model straight from the local snapshot.

## Data files (not in the repo)

`auth.json`, `.session_secret`, `state/`, `results.jsonl`, `teacher.log.jsonl`, `access.log` and
`server.log` are created at runtime and ignored by git — they hold credentials and personal progress.

## License

MIT — see `LICENSE`.
