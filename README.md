# EBT Notes (prototype, phone-first)

Three ways in, one transcript out. Then edit it and press Summarize.

1. **Online meeting:** paste a Google Meet or Zoom link. A bot joins and transcribes (Vexa). Speaker names come from the meeting itself.
2. **Upload:** any audio or video file (AssemblyAI, with speaker labels).
3. **Room mic:** record a physical meeting or lecture from the phone browser (AssemblyAI).

Summaries use the Google Gemini API. Hosting: Render free web service (Python).
The page can be installed to the phone home screen.

## Environment variables (Render, then Environment)

| Name | What it is |
|---|---|
| APP_PASSWORD | any password your team will use (protects your free credit) |
| ASSEMBLYAI_API_KEY | assemblyai.com, needed for Upload and Room mic |
| VEXA_API_KEY | vexa.ai, needed for Online meeting |
| GEMINI_API_KEY | aistudio.google.com, needed for Summarize |

Optional: `GEMINI_MODEL` (change it if the default model name stops working), `LANGUAGE_CODE` (default `en`; `auto` to auto-detect), `BOT_NAME` (name the bot shows in meetings).

You can start with only some keys: each mode works as soon as its own key is set.

## Not supported
WhatsApp calls (bots cannot join them, and phones block call-audio capture).

## Run on a computer instead
```
pip install -r requirements.txt
export APP_PASSWORD=... ASSEMBLYAI_API_KEY=... VEXA_API_KEY=... GEMINI_API_KEY=...
python app.py
```
Open http://localhost:5000 (or the port printed)
