# Behance Downloader — Troubleshooting & Decision Guide

> Read this **first** when `.github/workflows/behance.yml` or `behance_dl.py` fails. Proven stack (2026-08-22): **WARP (yt-dl.yml docker flags) → curl_cffi Firefox TLS → trimmed session cookies → GraphQL `/v3/graphql` → HTML fallback.**

---

## 0. One-paragraph summary

Behance’s Varnish edge treats GitHub Actions as a bot. A plain `urllib` GET of `/gallery/<id>/a` comes back **HTTP 429** no matter how long you wait, because the limit is keyed on **CPython’s TLS fingerprint (JA3) + datacenter IP**, not elapsed time. The working fetch is: start WARP the same way `yt-dl.yml` does, speak **Firefox TLS** via `curl_cffi`, send only **session cookies** (not the full Adobe dump), and pull gallery metadata from **`POST /v3/graphql`** (a different Varnish bucket than the HTML page). HTML scrape stays as fallback. The run that confirmed this downloaded gallery `252052029` (Ft-Scarlet) into `images/`.

---

## 1. First response when something breaks

1. Confirm the workflow checked out the commit you think it did (`actions/checkout` SHA). An old `behance_dl.py` on `main` looks identical in the Actions UI.
2. Read **Download images** (and **Start WARP** if that step ran). The first few log lines tell you which layer died:

| First lines | Meaning |
| --- | --- |
| `waiting 5s (attempt 1/6)` and `HTTP Error 429` | **Old script** (pre-`68d49cd`). Re-run on current `main`. |
| `HTTP via curl_cffi` then `HTTP 431` | Cookie / header blob too large. Trimming should fire; if it doesn’t, the secret is still huge. |
| Start WARP fails / no `WARP is active` | Container flags or health check — see [§3](#3-symptom--cause--fix-table). |
| `HTTP via curl_cffi` + `(via graphql)` + `+ … MB` | Working path. |

3. Change **one** layer, re-run, compare. 429/431 are environment-specific; bundling WARP + cookies + TLS changes makes the winner unattributable.

---

## 2. Cheat sheet

| Code / line | Meaning |
| --- | --- |
| `HTTP 429` | Rate limit. On GHA this is usually **JA3 + IP**, not “retry in 5s”. |
| `HTTP 431` | **Request Header Fields Too Large.** Cookie header and/or duplicated impersonate headers. |
| `HTTP 403` + `js_challenge_value=` | Varnish JS cookie challenge. Solvable; `behance_dl.py` sets the cookie and retries. |
| `WARP_PROXY` is `null` | WARP off or never became ready. Download then uses the runner IP. |
| `Loaded N cookie(s)` | `BEHANCE_COOKIES` secret parsed. N≈21 is a full browser export — too fat unless trimmed. |
| `(via graphql)` | Metadata came from `/v3/graphql`, not the HTML page. |
| `trimmed N cookie(s)` | Tracking cookies dropped so the header stays under ~6 KB. |
| `matureAccess` ≠ `allowed` | Age-gated. Needs a real Adobe session (`sso_sid` / `sso_uid` / `iat0`). |

**Cookie allowlist** (everything else is dropped):

`sso_sid`, `sso_uid`, `iat0`, `adobe_sso`, `bcp-sid`, `sid`, `user_sid`, `bcp`, `ilo0`, `js_challenge_value`, `gk_suid`

---

## 3. Symptom → cause → fix table

| Log signature | Cause | Fix |
| --- | --- | --- |
| `HTTP 429` on `> gallery: <id>` with `attempt N/6` and 5s/10s/40s/80s waits | Old `urllib` client + HTML page. Backoff (`5124ab9`) cannot outlast an IP/JA3 block. | Current `behance_dl.py`: curl_cffi + GraphQL. Confirm SHA is `8169d0b` or later. |
| `graphql unavailable (HTTP 431:)` then HTML also `HTTP 431` | Cookie header sent **twice** (session jar + explicit `Cookie:`) on top of Firefox impersonate headers + a 21-cookie Adobe dump. Varnish cap ~8 KB. | Trim to `COOKIE_KEEP`; do not set `Cookie` when using curl_cffi; impersonate owns UA/Accept/Sec-Fetch. |
| Start WARP fails | `docker run` missing `--sysctl net.ipv4.conf.all.src_valid_mark=1` (WARP image dies on GHA), and/or health check hit `behance.net` through a half-ready proxy (403/timeout). | Copy `yt-dl.yml`: those sysctls, `warp-data` volume, probe `cloudflare.com/cdn-cgi/trace` for `warp=(on\|plus)`. Fail the step if it never comes up. |
| `WARP_PROXY => null` in debug, download still runs | `use_warp` was defaulted **false** (`dbb9130`) *or* Start WARP never exported `WARP_PROXY`. | Default `use_warp: true` again. Proxy stays **step-scoped** (`ALL_PROXY` on Download only) — never `$GITHUB_ENV`, or checkout cleanup crashes on `socks5://`. |
| `age-restricted` / `matureAccess=logged-out` | Anonymous request; Behance returns zero image modules. | `BEHANCE_COOKIES` from a logged-in session (Netscape, JSON, or raw `Cookie:` header; plain or base64). |
| Workflow log looks “new” but still `attempt 1/6` | Actions ran a **pre-push** checkout. | Re-run on current `main`. New script logs `attempt 1/10`, `60s-hits=`, `HTTP via curl_cffi`. |

---

## 4. The working approach (layer by layer, with proof)

Order in the successful run:

1. **WARP SOCKS5 on `127.0.0.1:1080`** — same `docker run` as `yt-dl.yml` (`NET_ADMIN`, `src_valid_mark`, IPv6 sysctl, `warp-data` volume). Health check is Cloudflare’s trace, not Behance. *Proof:* Behance’s old probe failed Start WARP; YouTube’s flags + Cloudflare probe came up (`WARP is active`). Scoped `WARP_PROXY` onto Download only so post-job checkout does not see `socks5://`.

2. **`curl_cffi` Firefox impersonation** — JA3 matches a real Firefox. Fallbacks: system `curl` (HTTP/2), then urllib with TLS 1.3 Firefox ciphers. *Proof:* `urllib` on the runner 429’d the HTML gallery on the first request even after ~155s of exponential backoff (`5124ab9`). After curl_cffi: `HTTP via curl_cffi (Firefox impersonation)` and `Session primed` succeeded.

3. **Trimmed cookies** — load `BEHANCE_COOKIES`, keep the allowlist, drop AMCV/gki/analytics. curl_cffi uses the **session jar only** (no second `Cookie` header). *Proof:* the Firefox-TLS run that still had the full 21-cookie dump returned **431** on GraphQL and again on HTML (`HTTP 431:` empty body). After trim, downloads succeeded.

4. **GraphQL first** — `POST https://www.behance.net/v3/graphql` with `X-BCP` / `Origin` / `X-Requested-With`, queries `allModules` then paginated `modules(first: 50)`. *Proof:* GHA 429s were on `GET /gallery/<id>/a`. GraphQL is a different path; the good run used it for metadata. HTML `beconfig-store_state` scrape remains if GraphQL returns no image URLs (and is **not** used as a fallback for 429/431 — same IP/header budget).

5. **JS challenge** — 403 body containing `js_challenge_value=<hex>` is not a hard fail; set the cookie and retry (own budget, not a 429 attempt). Homepage warmup (`Session primed`) collects `gk_suid` + the challenge before the gallery call.

6. **Full-res images** — pick `source` / `max_3840` / `fs` / `hd` from `imageSizes.allAvailable` (path segment), not the `disp` preview. Files land in `tmp_images/`, then the existing zip/split/push steps write `images/`.

---

## 5. Pitfalls that cost time (do not repeat)

- **Backoff cannot fix a 429 that starts on attempt 1.** `5124ab9` waited ~4 minutes (5s → 120s cap, 6 tries). Every attempt was still 429. The block is JA3/IP, not a short window.
- **Do not diagnose from a stale checkout.** A run started before `68d49cd` still logged `attempt 1/6`. The debug dump (`WARP_PROXY => null`, correct URL, 21 cookies) was the old script. Always match log shape to SHA.
- **Do not health-check WARP via `behance.net`.** A half-ready proxy 403s/times out; the step looks “broken” even when Docker is fine. Use `https://cloudflare.com/cdn-cgi/trace` and `warp=(on|plus)`.
- **Do not `docker run` WARP without `src_valid_mark`.** That sysctl is why `yt-dl.yml` starts and why Behance’s thinner `docker run` died on the runner.
- **Do not export `ALL_PROXY=socks5h://…` to `$GITHUB_ENV`.** Node in `actions/checkout` post-job rejects the scheme (`Invalid URL protocol`). Keep `WARP_PROXY` and set `ALL_PROXY` only on the Download step.
- **Do not send Cookie twice with curl_cffi.** Session jar + `headers["Cookie"]` + impersonate’s own headers + a 21-cookie Adobe export = **431**. Homepage can still “prime” (just under the cap); the next GraphQL POST goes over.
- **Do not seed `gki` / `originalReferrer` / AMCV_*.** gallery-dl used to send a long `gki` string; it is not worth the header budget. `iat0` is large but required for mature galleries — drop other fat cookies first.

---

## 6. Tried and rejected, and why

| Idea | Why it failed / was dropped |
| --- | --- |
| Linear then exponential backoff on HTML GET | Inherited 429 on the first request; total wait ~155s still 429. |
| Default WARP **off** (`dbb9130`) | Avoided a *shared-exit* 429 theory, but the runner IP still 429’d, and the thin WARP step was itself broken. User confirmed WARP-on is what used to work. |
| Health-check Behance through SOCKS | False failure: Behance answers 403/timeout before WARP is “on”. |
| Longer `Retry-After` cap only | Behance often sends `Retry-After: 0` on 403/429. Honouring it does not create a wait. |
| GraphQL *instead of* fixing TLS | Necessary but not sufficient. GraphQL with a huge Cookie header became **431**. |
| Probing Behance from the local sandbox | Sandbox/proxy 403’d; GHA logs are the source of truth (same lesson as YouTube in `TROUBLESHOOTING.md` §6). |

---

## 7. Hard limits / ceilings

- **Shared WARP exits can still be 429-hot.** If many runners share the same Cloudflare pool, even Firefox TLS + GraphQL can inherit a block. Re-run (new exit) or a dedicated residential proxy. Code cannot mint a clean IP.
- **Mature galleries need a live Adobe session.** Trimming must keep `sso_sid` / `sso_uid` / `iat0`. If those expire, you get zero modules and a mature skip — not a 429.
- **GraphQL schema drift.** `allModules` vs `modules(first:)` is what worked in Aug 2026. If Adobe removes a field, the script falls back to HTML; if both 429, you are back at the IP ceiling.
- **`curl_cffi` wheel on the runner.** If pip cannot install it, the script falls back to `curl` then urllib. urllib is the path that 429’d. A failed `curl_cffi` install is a silent quality drop — check the Setup tools log.

---

## 8. Current state / loose ends

- **Working** (user-confirmed, 2026-08-22): WARP default on, Firefox TLS, cookie trim, GraphQL, push to `images/` (e.g. `images/252052029_Ft_Scarlet/`).
- **Commits:** `68d49cd` (TLS + GraphQL), `8169d0b` (WARP restore + 431 trim).
- **`use_warp` remains a flag** (default true). Turn it off only to A/B the runner IP.
- **`BEHANCE_COOKIES` is still a full browser dump.** Trimming happens at runtime; the secret does not need to be re-exported unless auth cookies expire.
- **`__pycache__/` and `links.txt`** in the repo root are local leftovers — not part of the fix.

---

## 9. Quick validation

Re-run **Behance Image Downloader** on `main` with WARP checked. A healthy Download images log looks like:

```
Loaded 21 cookie(s) from Cookie header string
Cookie header … B across 21 cookie(s)
HTTP via curl_cffi (Firefox impersonation)
  trimmed N cookie(s) (…); header now … B
Session primed
> gallery: <id>
  (via graphql)
  # <id> — <title>
    + <id>_01.jpg (n.n MB)
```

Start WARP must contain `WARP is active` and a `cdn-cgi/trace` dump with `warp=on` or `warp=plus`.

If images are missing: `COUNT` in the same step is `0` and the job fails. Check `/tmp/dl.log` in the Summary step (last 40 lines).
