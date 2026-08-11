#!/usr/bin/env python3
"""
Behance image downloader.

Handles Behance's Varnish JS-cookie challenge (403 -> js_challenge_value -> retry)
which gallery-dl does not, and pulls the full-resolution `source` variant rather
than the downscaled `disp` preview.

Accepted URL forms:
  https://www.behance.net/gallery/<id>/<slug>                  -> whole gallery
  https://www.behance.net/gallery/<id>/<slug>/modules/<mod_id> -> single image
  https://www.behance.net/<username>                           -> every gallery of that user

Usage:
  behance_dl.py -o OUTDIR URL [URL ...]
  behance_dl.py -o OUTDIR --limit 10 https://www.behance.net/username
"""

import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
ROOT = "https://www.behance.net"

_cj = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))
_opener.addheaders = [
    ("User-Agent", UA),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ("Accept-Language", "en-US,en;q=0.9"),
]


_mature_skips = 0


def log(msg):
    print(msg, flush=True)


def _install_socks_proxy():
    """Route all sockets through a SOCKS proxy if one is configured.

    urllib understands http:// proxies only — handed a socks5:// URL it would
    speak HTTP at the SOCKS port and fail. PySocks patches socket instead.
    """
    proxy = ""
    for var in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
                "PROXY", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            proxy = val
            break
    if not proxy or not proxy.lower().startswith("socks"):
        return  # no proxy, or a plain http:// proxy urllib handles natively

    parts = urllib.parse.urlparse(proxy)
    # Strip socks proxy vars so urllib doesn't also try to use them as HTTP proxies.
    for var in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
                "HTTP_PROXY", "http_proxy"):
        if os.environ.get(var, "").lower().startswith("socks"):
            os.environ.pop(var, None)

    try:
        import socks  # PySocks
        import socket
    except ImportError:
        log(f"! {proxy} requested but PySocks is not installed "
            f"(pip install PySocks); continuing WITHOUT the proxy")
        return

    kind = socks.SOCKS4 if "socks4" in parts.scheme.lower() else socks.SOCKS5
    # socks5h => resolve DNS through the proxy (avoids local DNS leaks).
    remote_dns = parts.scheme.lower().endswith("h") or kind == socks.SOCKS5
    socks.set_default_proxy(kind, parts.hostname, parts.port or 1080,
                            rdns=remote_dns, username=parts.username,
                            password=parts.password)
    socket.socket = socks.socksocket
    log(f"Routing traffic through {parts.hostname}:{parts.port or 1080} (SOCKS)")


def _set_challenge(token):
    _cj.set_cookie(http.cookiejar.Cookie(
        0, "js_challenge_value", token, None, False,
        ".behance.net", True, True, "/", True, True, 2 ** 31,
        False, None, None, {}))


def fetch(url, tries=4):
    """GET a URL, transparently solving the JS cookie challenge."""
    last = None
    for attempt in range(tries):
        try:
            with _opener.open(url, timeout=60) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            m = re.search(r"js_challenge_value=([a-f0-9]+)", body)
            if e.code == 403 and m:
                # Varnish handed us the challenge token; set it and retry.
                _set_challenge(m.group(1))
                continue
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise last or RuntimeError(f"failed to fetch {url}")


def gallery_data(gid):
    page = fetch(f"{ROOT}/gallery/{gid}/a")
    m = re.search(r'id="beconfig-store_state">(.*?)</script>', page, re.S)
    if not m:
        raise RuntimeError(f"gallery {gid}: could not locate embedded state")
    return json.loads(m.group(1))["project"]["project"]


def best_url(module):
    """Pick the highest-resolution variant for an image module."""
    sizes = module.get("imageSizes") or {}
    avail = sizes.get("allAvailable") or []
    if avail:
        best = max(avail, key=lambda x: x.get("width") or 0)
        if best.get("url"):
            return best["url"]
    for key in ("size_original", "size_max_3840", "size_1400",
                "size_max_1200", "size_disp"):
        entry = sizes.get(key)
        if isinstance(entry, dict) and entry.get("url"):
            return entry["url"]
    return module.get("src")


def _add_cookie(name, value, domain=".behance.net"):
    _cj.set_cookie(http.cookiejar.Cookie(
        0, name, value, None, False, domain, True, domain.startswith("."),
        "/", True, True, 2 ** 31, False, None, None, {}))


def _load_cookies(path):
    """Load cookies from a Netscape or JSON export. Returns count loaded."""
    raw = ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            raw = fh.read()
    except OSError as e:
        log(f"! could not read cookies file: {e}")
        return 0

    # Browser extensions often export JSON instead of Netscape format.
    stripped = raw.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(raw)
            items = data if isinstance(data, list) else data.get("cookies", [])
            n = 0
            for c in items:
                name, value = c.get("name"), c.get("value")
                if name and value is not None:
                    _add_cookie(name, value, c.get("domain") or ".behance.net")
                    n += 1
            log(f"Loaded {n} cookie(s) from JSON export")
            return n
        except Exception as e:  # noqa: BLE001
            log(f"! could not parse JSON cookies: {e}")
            return 0

    # Netscape format — tolerate a missing header line, which MozillaCookieJar
    # rejects outright even when every cookie line is well-formed.
    tmp = path
    if "# Netscape HTTP Cookie File" not in raw:
        tmp = os.path.join(os.path.dirname(os.path.abspath(path)),
                           ".behance_cookies_normalized.txt")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("# Netscape HTTP Cookie File\n" + raw)

    jar = http.cookiejar.MozillaCookieJar(tmp)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:  # noqa: BLE001
        log(f"! could not load cookies: {e}")
        return 0
    n = 0
    for c in jar:
        _cj.set_cookie(c)
        n += 1
    log(f"Loaded {n} cookie(s) from {path}")
    return n


def sanitize(name, fallback="untitled"):
    name = re.sub(r"[^\w\s.-]", "", str(name or "")).strip()
    name = re.sub(r"\s+", " ", name)
    return (name or fallback)[:120]


def images_for(data, module_id=None):
    """Return [(url, module_id)] for image modules, optionally a single one."""
    out = []
    for mod in data.get("modules") or []:
        if mod.get("__typename", "")[:-6].lower() != "image":
            continue
        if module_id and str(mod.get("id")) != str(module_id):
            continue
        url = best_url(mod)
        if url:
            out.append((url, mod.get("id")))
    return out


def download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        log(f"    = skip (exists) {os.path.basename(dest)}")
        return True
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": ROOT + "/"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                blob = r.read()
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, dest)
            log(f"    + {os.path.basename(dest)} ({len(blob) / 1048576:.1f} MB)")
            return True
        except Exception as e:  # noqa: BLE001 - report and retry
            log(f"    ! attempt {attempt + 1} failed: {e}")
            time.sleep(2 * (attempt + 1))
    return False


def do_gallery(gid, outdir, module_id=None):
    """Download one gallery. Returns (ok_count, fail_count)."""
    try:
        data = gallery_data(gid)
    except Exception as e:  # noqa: BLE001
        log(f"  ! gallery {gid}: {e}")
        return 0, 1

    title = data.get("name") or gid
    owners = ", ".join(
        o.get("display_name") or o.get("displayName") or "" for o in data.get("owners") or []
    ).strip(", ")
    log(f"  # {gid} — {title}" + (f" ({owners})" if owners else ""))

    imgs = images_for(data, module_id)
    if not imgs:
        access = data.get("matureAccess")
        if access and access != "allowed":
            # Behance returns zero modules for age-gated work unless logged in.
            global _mature_skips
            _mature_skips += 1
            log(f"    ! skipped: mature/age-restricted (matureAccess={access}); "
                f"requires BEHANCE_COOKIES from a logged-in account")
            return 0, 1
        log("    ! no image modules found")
        return 0, 1

    folder = os.path.join(outdir, f"{gid} {sanitize(title)}")
    os.makedirs(folder, exist_ok=True)

    ok = fail = 0
    for i, (url, _mid) in enumerate(imgs, 1):
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".jpg"
        dest = os.path.join(folder, f"{gid}_{i:02d}{ext}")
        if download(url, dest):
            ok += 1
        else:
            fail += 1
    return ok, fail


def user_galleries(username, limit=0):
    """Collect gallery ids from a profile page."""
    page = fetch(f"{ROOT}/{username}")
    ids, seen = [], set()
    for gid in re.findall(r"/gallery/(\d+)/", page):
        if gid not in seen:
            seen.add(gid)
            ids.append(gid)
    m = re.search(r'id="beconfig-store_state">(.*?)</script>', page, re.S)
    if m:
        try:
            for gid in re.findall(r'"id":\s*(\d{6,})', m.group(1)):
                if gid not in seen:
                    seen.add(gid)
                    ids.append(gid)
        except Exception:  # noqa: BLE001
            pass
    if limit:
        ids = ids[:limit]
    return ids


def parse(url):
    """Classify a URL -> ('gallery', gid, module_id) | ('user', name, None)."""
    url = url.strip()
    if not url:
        return None
    m = re.search(r"behance\.net/gallery/(\d+)(?:/[^/]*)?(?:/modules/(\d+))?", url)
    if m:
        return ("gallery", m.group(1), m.group(2))
    m = re.search(r"behance\.net/([A-Za-z0-9_-]+)/?$", url)
    if m and m.group(1) not in ("galleries", "search", "joblist"):
        return ("user", m.group(1), None)
    if re.fullmatch(r"\d+", url):
        return ("gallery", url, None)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="+")
    ap.add_argument("-o", "--output", default="behance_downloads")
    ap.add_argument("--limit", type=int, default=0,
                    help="max galleries per profile URL (0 = all)")
    ap.add_argument("--cookies", default=os.environ.get("BEHANCE_COOKIES", ""),
                    help="Netscape cookie file (for mature/age-gated galleries)")
    args = ap.parse_args()

    _install_socks_proxy()

    if args.cookies:
        # Loud failures here: a silently-ignored cookie file looks identical to
        # "no cookies", which surfaces later as a confusing mature-content skip.
        if not os.path.exists(args.cookies):
            log(f"! cookies file not found: {args.cookies}")
            return 2
        n = _load_cookies(args.cookies)
        if not n:
            return 2
        names = {c.name for c in _cj}
        if not names & {"bcp-sid", "sid", "adobe_sso", "user_sid", "bcp"}:
            log("! warning: cookies loaded but no Behance session cookie found; "
                "mature galleries will still be skipped. Export cookies for "
                "behance.net while logged in.")

    os.makedirs(args.output, exist_ok=True)

    targets = []
    for raw in args.urls:
        for part in re.split(r"[\s,]+", raw):
            p = parse(part)
            if p:
                targets.append(p)
            elif part.strip():
                log(f"! unrecognized URL: {part}")

    total_ok = total_fail = 0
    for kind, ident, module_id in targets:
        if kind == "user":
            log(f"> profile: {ident}")
            gids = user_galleries(ident, args.limit)
            log(f"  found {len(gids)} galleries")
            for gid in gids:
                ok, fail = do_gallery(gid, args.output)
                total_ok += ok
                total_fail += fail
                time.sleep(2)  # be polite; Behance rate-limits bursts
        else:
            log(f"> gallery: {ident}" + (f" module {module_id}" if module_id else ""))
            ok, fail = do_gallery(ident, args.output, module_id)
            total_ok += ok
            total_fail += fail

    log(f"\nDone. {total_ok} image(s) downloaded, {total_fail} failure(s).")
    if not total_ok and _mature_skips:
        log(f"\nAll {_mature_skips} gallery/galleries were age-restricted. "
            "Behance serves no images for these unless the request is "
            "authenticated — set the BEHANCE_COOKIES secret to a base64 "
            "cookies.txt exported while logged in to Behance.")
    return 0 if total_ok else 1


if __name__ == "__main__":
    sys.exit(main())
