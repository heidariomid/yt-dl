# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A YouTube/video downloader with two execution modes:
1. **Local web UI** — `server.py` + `download.sh`: a minimal Python HTTP server that serves an in-process HTML UI and shells out to `download.sh` per URL.
2. **GitHub Actions CI** — multiple workflows under `.github/workflows/` that run yt-dlp on GitHub's runners, route traffic through a Cloudflare WARP Docker sidecar (socks5://127.0.0.1:1080), and commit downloaded files back to the repo under `videos/` or `sound/`.

## Running locally

```bash
python3 server.py        # starts web UI at http://localhost:9876
./download.sh <URL> [quality] [output_dir]   # quality: best|1080|720|480|audio
```

Output goes to `~/Downloads/yt-dl` by default. Proxy is auto-detected from macOS system proxy (`scutil --proxy`); override with `PROXY=socks5://host:port ./download.sh`.

## Architecture

### Local mode (`server.py` + `download.sh`)

- `server.py` is a self-contained threaded HTTP server with all HTML/CSS/JS inlined as a Python string literal. No build step, no dependencies beyond stdlib.
- The `/download` endpoint streams Server-Sent Events (SSE) back to the browser — one `data:` JSON object per stdout line from `download.sh`.
- Cancel: the browser POSTs `/cancel`; the server sets `_cancel_flag` and calls `proc.terminate()` on the active subprocess.
- `links.txt` in the repo root pre-populates the URL textarea on page load. After a batch, failed URLs are written back to `links.txt`; successes clear it.
- `download.sh` tries up to 4 fallback player clients (`web`, `android`, `ios`, `tv_embedded`) in sequence, sleeping 3–8 s between retries.

### GitHub Actions mode (`.github/workflows/`)

Each workflow follows the same pattern:
1. Start a `caomingjun/warp` Docker container as a SOCKS5 proxy on port 1080.
2. The primary workflows (`yt-dl.yml`, `batch.yml`) also start `brainicum/bgutil-ytdlp-pot-provider` on port 4416 — this mints GVS PO tokens required for large/adaptive streams (4K, 401+251) to avoid HTTP 403 mid-download. It must use `--network host` so it can reach WARP on 127.0.0.1:1080.
3. Install latest yt-dlp from GitHub releases + `bgutil-ytdlp-pot-provider` pip plugin + Deno (for JS challenge solver: `--js-runtimes deno --remote-components ejs:github`).
4. Try multiple download methods (different `youtube:player_client` values) in order; validate result height against requested quality tier.
5. Split files >45 MB into `.zip`/`.z01`... parts (GitHub's recommended limit). Write a `README.md` with download links per video folder.
6. Push in batches of 5 files with rebase-retry loop to handle concurrent workflow runs.

**Key workflows:**
- `yt-dl.yml` — primary single/batch downloader with full JS solver + PO token stack
- `batch.yml` / `batch-fix.yml` — alternative batch downloaders (no JS solver; different client strategies)
- `audio.yml` — audio-only, single URL, faster setup
- `video-sub.yml` — downloads video + English subtitles
- `4k-improved.yml` / `small.yml` — specialized quality variants

### SABR / bot-detection notes

YouTube's SABR experiment (2025–2026) can cause some player clients to receive only 360p muxed streams even when requesting 1080p. The format selection uses `bv*+ba/b` with `-S "res,fps,vcodec,br"` to sort by resolution first, ensuring the tallest surviving stream is picked regardless of codec. The quality gate uses a threshold just under the requested tier (e.g. 1000p for "1080") to tolerate encoder variance, and keeps a "best-effort stash" so if no method clears the gate the highest file obtained is still saved.

`--audio-language orig` is passed on video downloads to prevent yt-dlp from defaulting to a dubbed audio track when the runner's WARP exit region (e.g. US) has a dubbed version.
