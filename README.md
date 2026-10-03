<<<<<<< HEAD
# ReelVault
=======
# ReelVault MVP
Share an Instagram reel/post to this app. It saves the link instantly, then in the background fetches the caption, asks Claude for a summary/tags/topic, and groups items by topic.

## Run
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...
    uvicorn app:app --host 0.0.0.0 --port 8000
Open http://localhost:8000 and paste a link to test.

## Share-sheet on your phone (Android)
The share target only works over HTTPS and when the PWA is installed.
1. Expose the app: `cloudflared tunnel --url http://localhost:8000` (or deploy to Render/Fly/Railway).
2. Open the HTTPS URL in Chrome on Android, menu > Install app.
3. In Instagram: Share > ReelVault. The link is saved and you can go straight back to the feed.

## Notes
- Fetching uses yt-dlp, then public og: tags. Private posts and login walls fail; they show as "failed" with a Retry button and the link is kept.
- Topics: Claude is shown your existing topics and reuses one when it fits, so similar saves group together.
- Search is keyword search over summary, caption, tags and topic.
- No auth: add a login or keep it private before exposing it publicly. It is a single-user tool.
- Next steps: Whisper transcription for audio, embeddings + semantic search, a native share extension for iOS.
>>>>>>> 53cb3fd (initial MVP)
