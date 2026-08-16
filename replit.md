# Telegram Image-to-Video Bot

 A Telegram bot that combines a user-provided image and audio track into an MP4 video and sends the result back.

## Run & Operate

- `python supervisor.py` — run the Telegram bot with automatic restart on failure
- `python main.py` — run the bot process directly for debugging
- `python -m compileall main.py` — validate Python syntax
- Required secret: `TELEGRAM_BOT_TOKEN` — token from @BotFather
- The health server listens on `PORT` (default `5000`), with `/` and `/healthz` endpoints.

## Stack

- Python 3.11
- `python-telegram-bot` for Telegram polling and media downloads
- Flask for the lightweight uptime-monitor health server
- MoviePy with FFmpeg for MP4 rendering
- Pillow for image decoding

## Where things live

- `main.py` — Telegram handlers, media state, rendering, and cleanup
- `pyproject.toml` / `uv.lock` — Python dependencies

## Architecture decisions

- The bot uses polling, so no public webhook endpoint is required.
- Each Telegram user gets an isolated temporary directory and pending image/audio pair.
- Rendering runs in a worker thread so the bot can continue responding to other users.
- Temporary source and output files are removed after every render attempt.
- A supervisor restarts the bot after unexpected process exits with exponential backoff.
- Abandoned pending uploads are automatically removed after one hour.
- A Flask health server runs in a daemon thread beside Telegram polling.
- An asyncio task self-pings the local health endpoint every four minutes.

## Product

- Users send an image and audio file in either order.
- The bot renders a video with the still image shown for the audio duration.
- The completed MP4 is returned to the same chat.
- `/reset` clears a pending pair before rendering.

## User preferences

None specified.

## Gotchas

- Audio is limited to 15 minutes per video.
- FFmpeg must be available for MoviePy to encode MP4 output.
- The Telegram bot token must remain in secrets and must not be committed to source.
- Run `python supervisor.py` rather than `python main.py` for long-running use.
- Configure an external uptime monitor to request the public app URL at `/` or `/healthz`.
- The internal self-ping uses `http://127.0.0.1:5000/healthz` when the workflow is on port 5000.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
