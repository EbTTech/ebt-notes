"""EBT Notes prototype (phone-first).

Three ways in, one transcript out:
  1. Online meeting: paste a Google Meet / Zoom / Teams link, a bot joins and transcribes (Recall.ai)
  2. Upload an audio or video file                                                   (AssemblyAI)
  3. Record a physical meeting or lecture with the phone mic                         (AssemblyAI)
Then edit the transcript and press Summarize                                         (Google Gemini)

Secrets (environment variables, never put them in the code):
  ASSEMBLYAI_API_KEY   needed for Upload and Room mic
  RECALL_API_KEY       needed for the Online meeting bot
  GEMINI_API_KEY       needed for the Summarize button
  APP_PASSWORD         optional but recommended, stops strangers using your free credit
Optional settings:
  RECALL_REGION        default us-east-1. Must match the region where you created the Recall key
                       (their pay-as-you-go signup uses us-west-2).
  GEMINI_MODEL         default gemini-3.5-flash-lite
  LANGUAGE_CODE        default "en" for uploads/recordings. "auto" = auto-detect.
  BOT_NAME             name the bot shows in the meeting, default "EBT Notes"
"""
import hmac
import os
from functools import wraps
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024  # 600 MB uploads

AAI_BASE = "https://api.assemblyai.com/v2"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
LANGUAGE_CODE = os.environ.get("LANGUAGE_CODE", "en")
RECALL_REGION = os.environ.get("RECALL_REGION", "us-east-1")
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


def recall_headers():
    key = os.environ.get("RECALL_API_KEY", "")
    if not key:
        raise RuntimeError("RECALL_API_KEY is not set on the server.")
    return {"Authorization": f"Token {key}", "Content-Type": "application/json"}


def recall_url(path):
    return f"https://{RECALL_REGION}.recall.ai/api/v1/{path}"


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


def build_turns_from_recall(segments):
    """Recall.ai transcript JSON -> same turn format. Speaker names come from the meeting itself."""
    turns = []
    for seg in segments or []:
        name = ((seg.get("participant") or {}).get("name") or "").strip() or "Unknown speaker"
        words = []
        for w in seg.get("words") or []:
            s = (w.get("start_timestamp") or {}).get("relative")
            e = (w.get("end_timestamp") or {}).get("relative")
            if s is None:
                continue
            words.append({"text": w.get("text", ""), "start": int(s * 1000), "end": int((e if e is not None else s) * 1000)})
        if not words:
            continue
        turns.append(
            {"speaker": name, "label": name, "start": words[0]["start"], "paragraphs": paragraphs_from_words(words)}
        )
    turns.sort(key=lambda t: t["start"])
    return merge_turns(turns)


ALLOWED_MEETING_HOSTS = (
    "meet.google.com", "zoom.us", "zoom.com", "teams.microsoft.com", "teams.live.com",
    "webex.com", "gotomeeting.com", "goto.com",
)


def valid_meeting_url(url):
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_MEETING_HOSTS)


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
        recall_key=bool(os.environ.get("RECALL_API_KEY")),
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


# ---------------------------------------------------------------- online meeting bot (Recall.ai)

@app.post("/api/bot/start")
@need_password
def bot_start():
    data = request.get_json(silent=True) or {}
    url = (data.get("meeting_url") or "").strip()
    if not valid_meeting_url(url):
        return jsonify(
            error="That does not look like a Google Meet, Zoom or Teams link. Paste the full link starting with https://"
        ), 400
    name = (data.get("bot_name") or DEFAULT_BOT_NAME).strip()[:60] or DEFAULT_BOT_NAME

    body = {
        "meeting_url": url,
        "bot_name": name,
        "recording_config": {"transcript": {"provider": {"recallai_streaming": {}}}},
    }
    try:
        r = requests.post(recall_url("bot"), headers=recall_headers(), json=body, timeout=60)
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    except requests.RequestException as e:
        return jsonify(error=f"Could not reach the meeting-bot service: {e}"), 502

    if r.status_code >= 400:
        return jsonify(error=f"Meeting-bot service said: {r.text[:300]}"), 502
    return jsonify(id=r.json()["id"])


BOT_MESSAGES = {
    "joining_call": "The bot is joining the meeting. If you are the host, admit it when it knocks.",
    "in_waiting_room": "The bot is in the waiting room. The host must admit it.",
    "in_call_not_recording": "The bot is in the meeting and getting ready to listen.",
    "in_call_recording": "The bot is in the meeting and transcribing.",
    "recording_permission_allowed": "The bot is in the meeting and transcribing.",
    "call_ended": "The meeting has ended. Preparing the transcript…",
}


@app.get("/api/bot/<bot_id>")
@need_password
def bot_status(bot_id):
    try:
        r = requests.get(recall_url(f"bot/{bot_id}/"), headers=recall_headers(), timeout=60)
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    except requests.RequestException as e:
        return jsonify(error=f"Could not reach the meeting-bot service: {e}"), 502

    if r.status_code >= 400:
        return jsonify(error=f"Meeting-bot service said: {r.text[:300]}"), 502

    bot = r.json()
    changes = bot.get("status_changes") or []
    last = changes[-1] if changes else {}
    code = last.get("code") or "joining_call"
    sub = last.get("sub_code")

    if code == "fatal":
        return jsonify(
            status="error",
            error="The bot could not join the meeting"
            + (f" ({sub})" if sub else "")
            + ". Check the link, and that the meeting has started and the host admits guests.",
        )

    if code == "done":
        url = None
        for rec in bot.get("recordings") or []:
            url = (((rec.get("media_shortcuts") or {}).get("transcript") or {}).get("data") or {}).get("download_url")
            if url:
                break
        if not url:
            return jsonify(status="error", error="The meeting ended but no transcript was produced. Was anyone speaking?")
        try:
            tr = requests.get(url, timeout=120)  # pre-signed URL, no auth header
            tr.raise_for_status()
            turns = build_turns_from_recall(tr.json())
        except (requests.RequestException, ValueError) as e:
            return jsonify(status="error", error=f"Could not download the transcript: {e}")
        if not turns:
            return jsonify(status="error", error="The transcript was empty. Was anyone speaking?")
        return jsonify(status="completed", turns=turns)

    return jsonify(status=code, message=BOT_MESSAGES.get(code, "Working…"))


@app.post("/api/bot/<bot_id>/leave")
@need_password
def bot_leave(bot_id):
    """Ask the bot to leave the call now (the transcript is then prepared as usual)."""
    try:
        r = requests.post(recall_url(f"bot/{bot_id}/leave_call/"), headers=recall_headers(), json={}, timeout=60)
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    except requests.RequestException as e:
        return jsonify(error=f"Could not reach the meeting-bot service: {e}"), 502
    if r.status_code >= 400:
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
