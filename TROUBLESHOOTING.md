# YouTube Downloader — Troubleshooting & Decision Guide

> Read this **first** when a `.github/workflows/*.yml` downloader breaks or behaves oddly. It records what we tried, what worked, what didn't, and _why_ — so we don't re-walk the whole path. `yt-dl.yml` is the proven reference implementation; everything here is about keeping it (and its siblings) working.

---

## 0. The one-paragraph summary

YouTube blocks/limits yt-dlp from **datacenter IPs** (GitHub runners + Cloudflare WARP). The working stack on the runner is: **WARP proxy → bgutil GVS PO-token provider (run with `--network host`) → Deno JS challenge solver → a multi-client ladder led by `web,mweb,android_vr` → a quality gate that aims high → a best-effort stash that never loses a video.** It reliably gets 1080p when YouTube serves it to a client that clears the bot check, gracefully keeps the best lower quality when it doesn't, and never hangs or skips. The remaining ceiling — when a specific video/session only exposes 720p to every working client — is the **IP reputation**, not the code. The only cure for _guaranteed_ HD is a residential proxy (costs money). See [§7](#7-the-real-ceiling-ip-not-code).

---

## 1. First response when something breaks

1. **Get the runner log** (the user triggers the workflow; you read the log). You cannot meaningfully reproduce on a local machine — see [§6](#6-why-local-tests-mislead).
2. **Find the decisive line** for each URL:
   ```
   [info] <id>: Downloading 1 format(s): 399+251
   ```
   The format IDs tell you the _real_ selected video+audio and therefore the true height/codec. Trust this line, not the download %s or the word "success".
3. **Classify the failure** by its log signature → jump to the matching row in [§3](#3-symptom--cause--fix-table).
4. **Change ONE layer, re-run, compare.** This problem is intermittent and environment-dependent; bundling changes makes cause impossible to attribute.

---

## 2. Format-ID cheat sheet

| ID              | Meaning                                                         |
| --------------- | --------------------------------------------------------------- | ----- | ----------------- |
| `18`            | **360p muxed** — the SABR "junk" fallback. Seeing this = gated. |
| `133`–`137`     | H.264 (avc1) video-only, 240p→1080p (`137`=1080p)               |
| `242`–`248`     | VP9 video-only (`248`=1080p)                                    |
| `394`–`401`     | AV1 (av01) video-only (`398`=720p, `399`=1080p, `401`=2160p/4K) |
| `140`           | AAC audio (m4a)                                                 | `251` | Opus audio (webm) |
| trailing `-drc` | dynamic-range-compressed audio variant — harmless               |

A `+` pair like `399+251` means separate video+audio that yt-dlp **merges** (losslessly remuxes) into one file. `401+251` = 4K.

---

## 3. Symptom → cause → fix table

| Log signature | What it means | Fix / lever |
| --- | --- | --- |
| `Sign in to confirm you're not a bot` | Bot check not cleared for that client/session | Deno JS solver; different client; different exit IP (re-run, or a no-proxy method). Intermittent per WARP IP. |
| `Downloading 1 format(s): 18` (360p) + `SABR-only streaming experiment` warning | **SABR gating** — high formats had their URLs stripped for that client | `android_vr` often escapes SABR; `web`/`mweb` get a **GVS PO token** (bgutil); ultimately a better IP |
| `HTTP Error 403: Forbidden` _partway through a large download_ | Adaptive stream needs a **GVS PO token** (separate streams 403 while muxed `18` still works) | bgutil PO-token provider (see [§4](#4-the-working-stack)). Small files sometimes finish before the 403 fires; big/4K reliably hit it |
| Repeating `Generating a gvs PO Token …` forever (hang) | bgutil container can't reach WARP to mint the token | **Run bgutil with `--network host`** (see [§5](#5-pitfalls-that-cost-us-hours)) |
| `This video is DRM protected` | `tv`/`tv_embedded` serve DRM'd formats without account cookies | Don't lead with `tv` on the runner; use the android_vr / web ladder |
| `No supported JavaScript runtime could be found … some formats may be missing` | Deno not installed/enabled → yt-dlp is blind to part of the ladder | Install Deno + pass `--js-runtimes deno --remote-components ejs` |
| Got 720p when 1080p "should" exist, across **every** method | That video didn't expose 1080p to any clearing client this session | Not a bug — see [§7](#7-the-real-ceiling-ip-not-code). Re-run or residential proxy. |

---

## 4. The working stack (what each layer does, and the proof it's needed)

We A/B-proved each layer earns its place — a stripped-down "simple" clone dropped a 1080p video to 360p. Order in `yt-dl.yml`:

1. **Cloudflare WARP** SOCKS5 on `127.0.0.1:1080` — the egress IP. The project's whole reason to run on GitHub (reaches YouTube from a blocked country). Keep it.
2. **bgutil GVS PO-token provider** (`brainicism/bgutil-ytdlp-pot-provider`) as a sidecar **with `--network host`**, + the `bgutil-ytdlp-pot-provider` pip plugin. Mints the GVS PO token that stops the **mid-download 403** on large/adaptive streams. yt-dlp auto-discovers it on `127.0.0.1:4416`. _Proof it's needed:_ without it, 4K (`401+251`) 403'd partway every time.
3. **Deno JS challenge solver** — install Deno, pass `--js-runtimes deno --remote-components ejs:github`. Solves the n-sig/BotGuard challenge so yt-dlp can **see the full format ladder**. _Proof it's needed:_ without it, yt-dlp warned "some formats may be missing" and a video dropped to 360p.
4. **Multi-client ladder + quality gate + best-effort stash**:
   - Method 1 leads with `--extractor-args "youtube:player_client=web,mweb,android_vr"` so yt-dlp picks whichever client clears the check and serves adaptive streams.
   - Fallbacks: `android_vr` alone (fast for short videos), `web`+solver, `tv_embedded`, then no-proxy variants (different exit IP).
   - **Quality gate** (ffprobe height): `1080`/`best` demand ~1080 (`MIN_HEIGHT=1000`, tolerant of encoder variance) so a 720p on an early method does NOT short-circuit — it forces other clients/IPs to be tried.
   - **Best-effort stash**: a gate-failing file is kept aside (largest wins); if no method clears the gate, save the best available rather than skipping.

Exact copy-paste blocks live in the skill: `~/.claude/skills/youtube-ytdlp-github-actions/references/working-config.md`.

---

## 5. Pitfalls that cost us hours (do NOT repeat)

- **bgutil hang → use `--network host`.** The provider is told to reach WARP at `socks5://127.0.0.1:1080`; inside a bridged container `127.0.0.1` is the container itself, not the host's WARP port, so token generation hangs forever (endless "Generating a gvs PO Token"). Host networking makes `127.0.0.1:1080` resolve to the runner's WARP port. (The plugin already bypasses `--proxy` for its own loopback call — that part was never the problem.)
- **`NO_PROXY` does nothing when `--proxy` is explicit.** We tested it: yt-dlp still routes through the SOCKS proxy. Don't rely on it.
- **`android_vr` carries no PO token** — great for short videos, but 403s on large ones. That's why method 1 is the multi-client+token path, not android_vr alone.
- **`tv`/`tv_embedded` = DRM** on the runner without cookies. Fine on a residential IP, useless as a runner lead.
- **`quality=best` must still aim high.** A naive "best accepts anything" stops at the first 360p. Treat `best` like `1080` in the gate.
- **Cookies were rejected.** GitHub secret masking corrupts multiline Netscape cookie files; the user tried it and it caused more trouble than it solved. Do not reintroduce a cookie approach unless explicitly asked.
- **Don't "simplify to match download.sh."** We tested an exact download.sh clone on the runner — it got _worse_ (360p, 403s, format-not-available). The complexity is load-bearing on a datacenter IP. download.sh only looks simpler-and-better because it runs on a residential IP.

---

## 6. Why local tests mislead

`download.sh` on the user's machine (residential Iran IP) behaves **opposite** to the runner:

|                    | Local (residential IP)          | Runner (datacenter + WARP) |
| ------------------ | ------------------------------- | -------------------------- |
| SABR gating        | rare                            | aggressive                 |
| `tv`/`tv_embedded` | works                           | DRM-locked                 |
| `android_vr`       | sometimes fails                 | often the one that works   |
| Result             | full 1080p/4K with trivial code | needs the whole stack      |

So a local probe **cannot** confirm a runner fix. Always validate on the runner. (You _can_ locally check format-selection plumbing, not bot-detection behavior.)

---

## 7. The real ceiling: IP, not code

This is the key mental model. **Quality is capped by the IP, not the workflow.**

- A residential IP is rarely SABR-gated → full quality with simple code.
- The _same code_ on a datacenter IP gets gated → 360p/720p sometimes.
- Proven repeatedly: same `yt-dl.yml`, same run, got 1080p on some videos and 720p on others purely by which WARP exit IP / session YouTube served.

**A 720p result is therefore often NOT a bug.** If the gate logs `Quality check FAILED: 720p < 1000p` for _every_ method and the selected format is e.g. `398+251` across android_vr AND web+token AND tv_embedded, that video simply did not expose 1080p to any clearing client on that session. The code is working: it tried everything and kept the best.

Levers for the IP ceiling, in order of effort:

1. **Re-run** — rolls a new WARP exit IP. Often enough. Free.
2. **WARP-restart-on-gate retry** — restart the container for a fresh IP and retry the ladder. Free, ~40s/restart. (Not currently implemented; held in reserve.)
3. **Residential/mobile proxy** via an optional `PROXY` secret — the only reliable cure for guaranteed HD. Costs money (~$2–15/mo). Keep WARP as the free default.

---

## 8. Quality / codec FAQ (settled with real ffprobe data)

**"The other repo's file is 855 MB and ours is 402 MB — is ours lower quality?"** No. ffprobe proved both are 1920×1080, same duration. The difference is codec:

|       | Other repo              | Ours                      |
| ----- | ----------------------- | ------------------------- |
| Codec | H.264 (avc1), 3168 kb/s | **AV1 (av01), 1627 kb/s** |
| Size  | 855 MB                  | 402 MB                    |

AV1 is ~2× more efficient than H.264 — **same 1080p, half the size.** That's why the difference is invisible on screen. Our resolution-first sort (`-S res:1080,fps,vcodec,br`) prefers the efficient codec; the other repo forces `[ext=mp4]` (H.264). The user is on a Mac with IINA and **prefers the smaller modern codec** — so keep the AV1/VP9 preference.

**"Why does it become .mp4 — do we transcode?"** No transcoding. YouTube serves 1080p as **separate** video-only + audio-only streams (`399+251`); yt-dlp **merges/remuxes** them into one container losslessly (the `Lavf…` encoder tag = ffmpeg remux, not re-encode). Container choice is yt-dlp's default; we don't force mp4 in yt-dl.

---

## 9. State of the repo

- **`yt-dl.yml`** — the proven, working downloader. This is the source of truth. Don't regress it.
- Other workflows (`fast.yml`, `4k.yml`, `4k-improved.yml`, `batch.yml`, `small.yml`, `large.yml`, `audio.yml`, `playlist.yml`, `video-sub.yml`, `check.yml`) **still use the old broken stack** (no bgutil, no Deno, old client ladders). To fix them, port the stack from `~/.claude/skills/youtube-ytdlp-github-actions/references/working-config.md`.
  - Note: `fast.yml` was _partially_ edited (bgutil step + Deno install + cleanup added) but its download ladder still uses the old `ios`/`web_creator` clients — it is **not** fully working. Either finish porting it or revert it before use.
- **`download.sh`** — local CLI; works because it runs on a residential IP. Not a template to copy onto the runner.
- Full methodology + exact snippets: `~/.claude/skills/youtube-ytdlp-github-actions/` (SKILL.md + references/).

---

## 10. Quick validation before committing a workflow change

These workflows only truly run on GitHub, but validate locally first:

```bash
# YAML parses + embedded bash is syntactically valid (PyYAML is usually absent on
# macOS; Ruby's YAML is reliably present):
ruby -ryaml -e "y=YAML.load_file('.github/workflows/yt-dl.yml'); \
  s=y['jobs']['download-youtube']['steps'].find{|x|x['name']=='Download YouTube Video'}; \
  File.write('/tmp/d.sh',s['run']); puts 'YAML OK'" && bash -n /tmp/d.sh && echo BASH OK
```

Then trigger on GitHub and read the `[info] Downloading 1 format(s):` line.
