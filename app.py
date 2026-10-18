"""EBT Notes prototype (phone-first).

Three ways in, one transcript out:
  1. Online meeting: paste a Google Meet or Zoom link, a bot joins and transcribes (Vexa)
  2. Upload an audio or video file                                                   (AssemblyAI)
  3. Record a physical meeting or lecture with the phone mic                         (AssemblyAI)
Then edit the transcript and press Summarize                                         (Google Gemini)

Secrets (environment variables, never put them in the code):
  ASSEMBLYAI_API_KEY   needed for Upload and Room mic
  VEXA_API_KEY         needed for the Online meeting bot (from vexa.ai)
  GEMINI_API_KEY       needed for the Summarize button
  APP_PASSWORD         optional but recommended, stops strangers using your free credit
Optional settings:
  VEXA_API_BASE        default https://api.cloud.vexa.ai
  GEMINI_MODEL         default gemini-3.5-flash-lite
  LANGUAGE_CODE        default "en" for uploads/recordings. "auto" = auto-detect.
  BOT_NAME             name the bot shows in the meeting, default "EBT Notes"
"""
import hmac
import os
from functools import wraps
import re
from urllib.parse import parse_qs, urlparse

import requests
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024  # 600 MB uploads

AAI_BASE = "https://api.assemblyai.com/v2"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
LANGUAGE_CODE = os.environ.get("LANGUAGE_CODE", "en")
VEXA_BASE = os.environ.get("VEXA_API_BASE", "https://api.cloud.vexa.ai").rstrip("/")
DEFAULT_BOT_NAME = os.environ.get("BOT_NAME", "EBT Notes")

# A pause longer than this inside one speaker's turn starts a new paragraph...
PARAGRAPH_GAP_MS = 1800
# ...and so does reaching this many words, at the next sentence end.
PARAGRAPH_MAX_WORDS = 90


# ---------------------------------------------------------------- helpers

def need_password(fn):
    """If APP_PASSWORD is set, every API call must send it in X-App-Password."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = os.environ.get("APP_PASSWORD", "")
        if expected:
            given = request.headers.get("X-App-Password", "")
            if not hmac.compare_digest(given.encode(), expected.encode()):
                return jsonify(error="Wrong or missing password."), 401
        return fn(*args, **kwargs)

    return wrapper


def aai_headers():
    key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not key:
        raise RuntimeError("ASSEMBLYAI_API_KEY is not set on the server.")
    return {"authorization": key}


def vexa_headers():
    key = os.environ.get("VEXA_API_KEY", "")
    if not key:
        raise RuntimeError("VEXA_API_KEY is not set on the server.")
    return {"X-API-Key": key, "Content-Type": "application/json"}


def vexa_url(path):
    return f"{VEXA_BASE}/{path.lstrip('/')}"


def paragraphs_from_words(words):
    """words: [{text, start, end}] in milliseconds -> list of paragraph strings."""
    paragraphs, current, count, prev_end = [], [], 0, None
    for w in words:
        gap = (w["start"] - prev_end) if prev_end is not None else 0
        sentence_end = bool(current) and current[-1].rstrip().endswith((".", "?", "!"))
        if current and sentence_end and (gap > PARAGRAPH_GAP_MS or count >= PARAGRAPH_MAX_WORDS):
            paragraphs.append(" ".join(current))
            current, count = [], 0
        current.append(w["text"])
        count += 1
        prev_end = w["end"]
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def merge_turns(turns):
    """Join back-to-back turns by the same speaker."""
    merged = []
    for t in turns:
        if merged and merged[-1]["speaker"] == t["speaker"]:
            merged[-1]["paragraphs"].extend(t["paragraphs"])
        else:
            merged.append(t)
    return merged


def build_turns(transcript):
    """AssemblyAI transcript -> [{speaker, label, start, paragraphs}]."""
    turns = []
    utterances = transcript.get("utterances") or []

    if not utterances:
        text = (transcript.get("text") or "").strip()
        if text:
            turns.append({"speaker": "A", "label": "Speaker A", "start": 0, "paragraphs": [text]})
        return turns

    for utt in utterances:
        words = [
            {"text": w["text"], "start": w["start"], "end": w["end"]} for w in (utt.get("words") or [])
        ]
        paragraphs = paragraphs_from_words(words) or [(utt.get("text") or "").strip()]
        sp = utt.get("speaker") or "A"
        turns.append(
            {"speaker": sp, "label": f"Speaker {sp}", "start": utt.get("start", 0), "paragraphs": paragraphs}
        )
    return merge_turns(turns)


def build_turns_from_vexa(segments):
    """Vexa transcript segments -> same turn format. Speaker names come from the meeting itself.

    Each segment: {speaker, text, start (seconds), end (seconds), completed, ...}
    """
    segs = [s for s in (segments or []) if (s.get("text") or "").strip() and s.get("completed", True) is not False]
    segs.sort(key=lambda s: s.get("start") or 0)
    turns, prev_end = [], None
    for s in segs:
        speaker = (s.get("speaker") or "").strip() or "Unknown speaker"
        start = int((s.get("start") or 0) * 1000)
        end = int((s.get("end") or s.get("start") or 0) * 1000)
        text = s["text"].strip()
        same = bool(turns) and turns[-1]["speaker"] == speaker
        if same:
            gap = start - prev_end if prev_end is not None else 0
            last = turns[-1]["paragraphs"]
            words_in_last = len(last[-1].split())
            if gap > PARAGRAPH_GAP_MS or (words_in_last >= PARAGRAPH_MAX_WORDS and last[-1].rstrip().endswith((".", "?", "!"))):
                last.append(text)
            else:
                last[-1] = last[-1] + " " + text
        else:
            turns.append({"speaker": speaker, "label": speaker, "start": start, "paragraphs": [text]})
        prev_end = end
    return turns


MEET_RE = re.compile(r"^/([a-z]{3}-[a-z]{4}-[a-z]{3})/?$", re.I)
ZOOM_RE = re.compile(r"^/(?:j|wc/join)/(\d{9,12})/?$")


def parse_meeting_url(url):
    """Return (platform, native_meeting_id, passcode) or None. Supports Google Meet and Zoom."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme != "https":
        return None
    host = (p.hostname or "").lower()
    if host == "meet.google.com":
        m = MEET_RE.match(p.path)
        return ("google_meet", m.group(1).lower(), None) if m else None
    if host == "zoom.us" or host.endswith(".zoom.us") or host == "zoom.com" or host.endswith(".zoom.com"):
        m = ZOOM_RE.match(p.path)
        if m:
            pwd = (parse_qs(p.query).get("pwd") or [None])[0]
            return ("zoom", m.group(1), pwd)
    return None


# ---------------------------------------------------------------- pages

@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(BASE_DIR, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(BASE_DIR, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/<name>.png")
def icons(name):
    # Only the three app icons are served; nothing else in this folder is public.
    if name not in ("icon-192", "icon-512", "icon-maskable-512"):
        return jsonify(error="Not found"), 404
    return send_from_directory(BASE_DIR, name + ".png")


@app.get("/api/health")
def health():
    return jsonify(
        ok=True,
        assemblyai_key=bool(os.environ.get("ASSEMBLYAI_API_KEY")),
        bot_key=bool(os.environ.get("VEXA_API_KEY")),
        gemini_key=bool(os.environ.get("GEMINI_API_KEY")),
        password_required=bool(os.environ.get("APP_PASSWORD")),
        bot_name=DEFAULT_BOT_NAME,
    )


@app.post("/api/check")
@need_password
def check():
    """Lets the page test the password before the user does anything."""
    return jsonify(ok=True)


# ---------------------------------------------------------------- uploads and room mic (AssemblyAI)

@app.post("/api/transcribe")
@need_password
def transcribe():
    """Receive an audio/video file, send it to AssemblyAI, return a job id."""
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file received."), 400

    try:
        headers = aai_headers()
        up = requests.post(
            f"{AAI_BASE}/upload",
            headers={**headers, "content-type": "application/octet-stream"},
            data=f.stream,
            timeout=900,
        )
        up.raise_for_status()
        upload_url = up.json()["upload_url"]

        body = {
            "audio_url": upload_url,
            "speaker_labels": True,
            "punctuate": True,
            "format_text": True,
        }
        if LANGUAGE_CODE and LANGUAGE_CODE.lower() != "auto":
            body["language_code"] = LANGUAGE_CODE

        speakers = request.form.get("speakers", "").strip()
        if speakers.isdigit() and int(speakers) > 0:
            body["speakers_expected"] = int(speakers)

        r = requests.post(f"{AAI_BASE}/transcript", headers=headers, json=body, timeout=60)
        if r.status_code >= 400:
            return jsonify(error=f"Speech service said: {r.text[:300]}"), 502
        return jsonify(id=r.json()["id"])
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    except requests.RequestException as e:
        return jsonify(error=f"Could not reach the speech service: {e}"), 502


@app.get("/api/status/<job_id>")
@need_password
def status(job_id):
    try:
        r = requests.get(f"{AAI_BASE}/transcript/{job_id}", headers=aai_headers(), timeout=60)
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    except requests.RequestException as e:
        return jsonify(error=f"Could not reach the speech service: {e}"), 502

    if r.status_code >= 400:
        return jsonify(error=f"Speech service said: {r.text[:300]}"), 502

    t = r.json()
    st = t.get("status")
    if st == "completed":
        return jsonify(status="completed", turns=build_turns(t), duration=t.get("audio_duration"))
    if st == "error":
        return jsonify(status="error", error=t.get("error", "Transcription failed."))
    return jsonify(status=st or "processing")


# ---------------------------------------------------------------- online meeting bot (Vexa)

@app.post("/api/bot/start")
@need_password
def bot_start():
    data = request.get_json(silent=True) or {}
    url = (data.get("meeting_url") or "").strip()
    parsed = parse_meeting_url(url)
    if not parsed:
        return jsonify(
            error="That does not look like a Google Meet or Zoom link. Paste the full link starting with https:// "
                  "(Google Meet links look like https://meet.google.com/abc-defg-hij)."
        ), 400
    platform, native_id, passcode = parsed
    name = (data.get("bot_name") or DEFAULT_BOT_NAME).strip()[:60] or DEFAULT_BOT_NAME

    body = {"platform": platform, "native_meeting_id": native_id, "bot_name": name}
    if platform == "zoom":
        body["meeting_url"] = url
        if passcode:
            body["passcode"] = passcode

    try:
        r = requests.post(vexa_url("bots"), headers=vexa_headers(), json=body, timeout=60)
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    except requests.RequestException as e:
        return jsonify(error=f"Could not reach the meeting-bot service: {e}"), 502

    if r.status_code == 409:
        # A bot is already in this meeting: just keep following it.
        return jsonify(id=f"{platform}:{native_id}", note="A bot is already in this meeting.")
    if r.status_code in (401, 403):
        return jsonify(error=f"The meeting-bot service refused the request ({r.status_code}). Check VEXA_API_KEY, and your plan limits."), 502
    if r.status_code >= 400:
        return jsonify(error=f"Meeting-bot service said: {r.text[:300]}"), 502
    return jsonify(id=f"{platform}:{native_id}")


BOT_MESSAGES = {
    "requested": "The bot has been requested and is getting ready.",
    "joining": "The bot is joining the meeting. If you are the host, admit it when it knocks.",
    "awaiting_admission": "The bot is waiting to be admitted. The host must let it in.",
    "needs_help": "The bot needs help joining. Check the meeting and admit it.",
    "active": "The bot is in the meeting and transcribing.",
    "stopping": "The bot is leaving. Preparing the transcript…",
}


def split_bot_id(bot_id):
    platform, _, native = bot_id.partition(":")
    if platform not in ("google_meet", "zoom") or not native or "/" in native:
        return None
    return platform, native


def find_meeting(platform, native_id):
    """Look up this meeting in the user's meeting list. Returns the record or None."""
    r = requests.get(vexa_url("meetings"), headers=vexa_headers(), params={"limit": 50}, timeout=60)
    if r.status_code >= 400:
        return None
    data = r.json()
    items = data if isinstance(data, list) else (data.get("meetings") or data.get("items") or data.get("data") or [])
    for m in items:
        if isinstance(m, dict) and m.get("platform") == platform and str(m.get("native_meeting_id")) == native_id:
            return m  # newest first is the usual order, so the first match is the latest
    return None


@app.get("/api/bot/<bot_id>")
@need_password
def bot_status(bot_id):
    ids = split_bot_id(bot_id)
    if not ids:
        return jsonify(error="Unknown meeting id."), 400
    platform, native_id = ids
    final = request.args.get("final") == "1"  # the page sets this after you press "Stop bot now"

    try:
        meeting = find_meeting(platform, native_id)
        status = (meeting or {}).get("status")

        if status == "failed":
            reason = (meeting.get("data") or {}).get("completion_reason") or meeting.get("completion_reason")
            return jsonify(
                status="error",
                error="The bot could not stay in the meeting" + (f" ({reason})" if reason else "")
                + ". Check the link, that the meeting has started, and that the host admitted the bot.",
            )

        if status == "completed" or final:
            tr = requests.get(vexa_url(f"transcripts/{platform}/{native_id}"), headers=vexa_headers(), timeout=120)
            if tr.status_code == 404:
                if status == "completed":
                    return jsonify(status="error", error="The meeting ended but no transcript was found. Was anyone speaking?")
                return jsonify(status="stopping", message=BOT_MESSAGES["stopping"])
            if tr.status_code >= 400:
                return jsonify(status="error", error=f"Meeting-bot service said: {tr.text[:300]}")
            turns = build_turns_from_vexa(tr.json().get("segments"))
            if turns:
                return jsonify(status="completed", turns=turns)
            if status == "completed":
                return jsonify(status="error", error="The transcript was empty. Was anyone speaking?")
            return jsonify(status="stopping", message=BOT_MESSAGES["stopping"])
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    except (requests.RequestException, ValueError) as e:
        return jsonify(error=f"Could not reach the meeting-bot service: {e}"), 502

    code = status or "requested"
    return jsonify(status=code, message=BOT_MESSAGES.get(code, "Working…"))


@app.post("/api/bot/<bot_id>/leave")
@need_password
def bot_leave(bot_id):
    """Ask the bot to leave the call now. The transcript is then collected as usual."""
    ids = split_bot_id(bot_id)
    if not ids:
        return jsonify(error="Unknown meeting id."), 400
    platform, native_id = ids
    try:
        r = requests.delete(vexa_url(f"bots/{platform}/{native_id}"), headers=vexa_headers(), timeout=60)
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    except requests.RequestException as e:
        return jsonify(error=f"Could not reach the meeting-bot service: {e}"), 502
    if r.status_code >= 400 and r.status_code != 404:
        return jsonify(error=f"Meeting-bot service said: {r.text[:300]}"), 502
    return jsonify(ok=True)


# ---------------------------------------------------------------- summary (Gemini)

SUMMARY_PROMPT = """You are a careful meeting secretary. Below is the transcript of a meeting, lecture or conference. \
Speaker labels may have been renamed by the user. Write in clear, simple English.

Produce exactly these sections, using these headings:

## Overview
3 to 5 sentences: what the meeting was about and the overall outcome.

## Key points
Bullet list of the main things discussed, grouped by topic.

## Decisions made
Bullet list. Write "None recorded" if there were none.

## Action items
Bullet list in the form "Person: task (deadline if mentioned)". Write "None recorded" if there were none.

## Open questions
Bullet list of things left unresolved. Write "None" if there were none.

Rules: use only what is in the transcript, do not invent names, numbers or deadlines. \
Words may be mis-transcribed, especially names and Nigerian Pidgin; if a word is unclear, say so instead of guessing.

TRANSCRIPT:
"""


@app.post("/api/summarize")
@need_password
def summarize():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="There is no transcript text to summarize."), 400

    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return jsonify(error="GEMINI_API_KEY is not set on the server."), 500

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": SUMMARY_PROMPT + text}]}],
        "generationConfig": {"temperature": 0.3},
    }
    try:
        r = requests.post(url, headers={"x-goog-api-key": key}, json=payload, timeout=180)
    except requests.RequestException as e:
        return jsonify(error=f"Could not reach the summary service: {e}"), 502

    if r.status_code >= 400:
        return jsonify(error=f"Summary service said: {r.text[:300]}"), 502

    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        summary = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, ValueError):
        return jsonify(error="The summary service returned an empty answer. Try again."), 502
    return jsonify(summary=summary)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 7860)), debug=False)
