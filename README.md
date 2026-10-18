---
title: EBT Notes
emoji: 📝
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# EBT Notes (prototype, phone-first)

Three ways in, one transcript out. Then edit it and press Summarize.

1. **Online meeting:** paste a Google Meet, Zoom or Teams link. A bot joins and transcribes (Recall.ai). Speaker names come from the meeting itself.
2. **Upload:** any audio or video file (AssemblyAI, with speaker labels).
3. **Room mic:** record a physical meeting or lecture from the phone browser (AssemblyAI).

Summaries use the Google Gemini API. Hosting: Hugging Face Spaces (free CPU Space).
The page can be installed to the phone home screen.

## Secrets (Space Settings, then Variables and secrets, then New secret)

| Name | What it is |
|---|---|
| APP_PASSWORD | any password your team will use (protects your free credit) |
| ASSEMBLYAI_API_KEY | assemblyai.com, needed for Upload and Room mic |
| RECALL_API_KEY | recall.ai, needed for Online meeting |
| GEMINI_API_KEY | aistudio.google.com, needed for Summarize |
| RECALL_REGION | the region where you created the Recall key, e.g. `us-west-2` or `us-east-1` (default `us-east-1`) |

Optional: `GEMINI_MODEL` (change it if the default model name stops working), `LANGUAGE_CODE` (default `en`; `auto` to auto-detect), `BOT_NAME` (name the bot shows in meetings).

You can start with only some keys: each mode works as soon as its own key is set.

## Not supported
WhatsApp calls (bots cannot join them, and phones block call-audio capture).

## Run on a computer instead
```
pip install -r requirements.txt
export APP_PASSWORD=... ASSEMBLYAI_API_KEY=... RECALL_API_KEY=... GEMINI_API_KEY=... RECALL_REGION=us-west-2
python app.py
```
Open http://localhost:7860
