#!/usr/bin/env python3
"""
Behance image downloader.

Handles Behance's Varnish JS-cookie challenge (403 -> js_challenge_value -> retry)
which gallery-dl does not, and pulls the full-resolution `source` variant rather
than the downscaled `disp` preview.

HTTP clients (first available wins):
  1. curl_cffi impersonating Firefox — JA3/TLS that Varnish does not 429
  2. system curl (HTTP/2)
  3. urllib with TLS 1.3 + Firefox ciphers

Gallery metadata prefers POST /v3/graphql. GitHub Actions IPs get HTTP 429 on
the HTML gallery page; the GraphQL endpoint is a different Varnish bucket.

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
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) "
      "Gecko/20100101 Firefox/140.0")
ROOT = "https://www.behance.net"
GRAPHQL = ROOT + "/v3/graphql"
BCP_DEFAULT = "4c34489d-914c-46cd-b44c-dfd0e661136d"

# gallery-dl's default session cookies — Varnish 403s gallery pages without them.
# Keep this tiny: a full Adobe cookie export (iat0 JWT + AMCV_* + gki) blows
# past Varnish's header budget and comes back as HTTP 431.
BROWSER_COOKIES = {
    "bcp": BCP_DEFAULT,
    "ilo0": "true",
    "gk_suid": "14118261",
}

COOKIE_KEEP = {
    "sso_sid", "sso_uid", "iat0", "adobe_sso", "bcp-sid", "sid", "user_sid",
    "bcp", "ilo0", "js_challenge_value", "gk_suid",
}
COOKIE_BUDGET = 6 * 1024  # bytes; Adobe/Varnish 431s around 8 KB of headers

# 429 windows on datacenter IPs last minutes. 15s doubling to 180s across
# 10 attempts is ~20 minutes and still honours a longer Retry-After.
BACKOFF_BASE = 15
BACKOFF_CAP = 180
RETRY_AFTER_CAP = 600
DEFAULT_TRIES = 10

HTML_HEADERS = (
    ("User-Agent", UA),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ("Accept-Language", "en-US,en;q=0.5"),
    ("Referer", ROOT + "/"),
    ("Upgrade-Insecure-Requests", "1"),
    ("Sec-Fetch-Dest", "document"),
    ("Sec-Fetch-Mode", "navigate"),
    ("Sec-Fetch-Site", "none"),
)

GQL_HEADERS = (
    ("User-Agent", UA),
    ("Accept", "application/json"),
    ("Accept-Language", "en-US,en;q=0.5"),
    ("Origin", ROOT),
    ("Referer", ROOT + "/"),
    ("X-Requested-With", "XMLHttpRequest"),
    ("Content-Type", "application/json"),
    ("Sec-Fetch-Dest", "empty"),
    ("Sec-Fetch-Mode", "cors"),
    ("Sec-Fetch-Site", "same-origin"),
)

# Prefer allModules (flat list). Fall back to the paginated connection.
GQL_PROJECT_QUERIES = (
    """
    query GetProject($id: Int!) {
      project(id: $id) {
        id
        name
        matureAccess
        owners { displayName }
        allModules {
          __typename
          ... on ImageModule {
            id
            imageSizes { allAvailable { url width } }
          }
        }
      }
    }
    """,
    """
    query GetProject($id: Int!, $after: String) {
      project(id: $id) {
        id
        name
        matureAccess
        owners { displayName }
        modules(first: 50, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes {
            __typename
            ... on ImageModule {
              id
              imageSizes { allAvailable { url width } }
            }
          }
        }
      }
    }
    """,
)

_cj = http.cookiejar.CookieJar()
_opener = None
_http_backend = None  # "curl_cffi" | "curl" | "urllib"
_cf_session = None
_cf_impersonate = None
_socks_proxy = ""
_cookie_file = None

_mature_skips = 0


def log(msg):
    print(msg, flush=True)


def _install_socks_proxy():
    """Remember a SOCKS proxy and patch urllib's socket layer if needed.

    urllib understands http:// proxies only — handed a socks5:// URL it would
    speak HTTP at the SOCKS port and fail. PySocks patches socket instead.
    curl / curl_cffi get the URL passed explicitly, so env vars are stripped
    to keep urllib from misusing them.
    """
    global _socks_proxy
    proxy = ""
    for var in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
                "PROXY", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            proxy = val
            break
    if not proxy or not proxy.lower().startswith("socks"):
        return

    _socks_proxy = proxy
    parts = urllib.parse.urlparse(proxy)
    for var in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
                "HTTP_PROXY", "http_proxy"):
        if os.environ.get(var, "").lower().startswith("socks"):
            os.environ.pop(var, None)

    try:
        import socks  # PySocks
        import socket
    except ImportError:
        log(f"! {proxy} requested but PySocks is not installed "
            f"(pip install PySocks); urllib fallback will be unproxied")
        return

    kind = socks.SOCKS4 if "socks4" in parts.scheme.lower() else socks.SOCKS5
    remote_dns = parts.scheme.lower().endswith("h") or kind == socks.SOCKS5
    socks.set_default_proxy(kind, parts.hostname, parts.port or 1080,
                            rdns=remote_dns, username=parts.username,
                            password=parts.password)
    socket.socket = socks.socksocket
    log(f"Routing traffic through {parts.hostname}:{parts.port or 1080} (SOCKS)")


def _ssl_context():
    """TLS 1.3-only Firefox ciphers — urllib's default JA3 is what Varnish 429s."""
    ctx = ssl.create_default_context()
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.set_ciphers(
            "TLS_AES_128_GCM_SHA256:"
            "TLS_CHACHA20_POLY1305_SHA256:"
            "TLS_AES_256_GCM_SHA384"
        )
    except ssl.SSLError:
        pass
    return ctx


def _add_cookie(name, value, domain=".behance.net"):
    _cj.set_cookie(http.cookiejar.Cookie(
        0, name, value, None, False, domain, True, domain.startswith("."),
        "/", True, True, 2 ** 31, False, None, None, {}))


def _set_challenge(token):
    _add_cookie("js_challenge_value", token)


def _cookie_header():
    return "; ".join(f"{c.name}={c.value}" for c in _cj)


def _bcp():
    for c in _cj:
        if c.name == "bcp":
            return c.value
    return BCP_DEFAULT


def _seed_browser_cookies():
    have = {c.name for c in _cj}
    for name, value in BROWSER_COOKIES.items():
        if name not in have:
            _add_cookie(name, value)


def _drop_cookie(name):
    for c in list(_cj):
        if c.name == name:
            _cj.clear(c.domain, c.path, c.name)


def _refresh_cf_cookies():
    if _cf_session is None:
        return
    try:
        _cf_session.cookies.clear()
    except Exception:  # noqa: BLE001
        pass
    for c in _cj:
        _cf_session.cookies.set(c.name, c.value, domain=c.domain or ".behance.net")


def _trim_cookies(aggressive=False):
    """Drop tracking cookies so the Cookie header stays under Varnish's limit."""
    keep = set(COOKIE_KEEP)
    if aggressive:
        keep = {"sso_sid", "sso_uid", "iat0", "bcp", "ilo0", "js_challenge_value"}
    dropped = []
    for c in list(_cj):
        if c.name not in keep:
            dropped.append(c.name)
            _drop_cookie(c.name)
    header = _cookie_header()
    if len(header) > COOKIE_BUDGET:
        protected = {"js_challenge_value", "bcp", "ilo0", "sso_sid"}
        for c in sorted(_cj, key=lambda x: len(x.value or ""), reverse=True):
            if c.name in protected:
                continue
            dropped.append(c.name)
            _drop_cookie(c.name)
            if len(_cookie_header()) <= COOKIE_BUDGET:
                break
    _refresh_cf_cookies()
    if dropped:
        log(f"  trimmed {len(dropped)} cookie(s) ({', '.join(dropped)}); "
            f"header now {len(_cookie_header())} B")
    return bool(dropped)


def _write_cookie_file():
    """Netscape cookie file for curl -b/-c."""
    global _cookie_file
    if not _cookie_file:
        fd, _cookie_file = tempfile.mkstemp(prefix="behance_cj_", suffix=".txt")
        os.close(fd)
    with open(_cookie_file, "w", encoding="utf-8") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        for c in _cj:
            domain = c.domain or ".behance.net"
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.path or "/"
            secure = "TRUE" if c.secure else "FALSE"
            expires = str(int(c.expires or 2 ** 31))
            value = (c.value or "").replace("\t", "")
            fh.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{c.name}\t{value}\n")
    return _cookie_file


def _reload_cookie_file():
    if not _cookie_file or not os.path.exists(_cookie_file):
        return
    jar = http.cookiejar.MozillaCookieJar(_cookie_file)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:  # noqa: BLE001
        return
    for c in jar:
        _cj.set_cookie(c)


def _init_http():
    """Pick an HTTP backend and seed the browser cookies Varnish expects."""
    global _http_backend, _cf_session, _cf_impersonate, _opener
    _seed_browser_cookies()
    _trim_cookies()

    try:
        from curl_cffi import requests as cf
        sess = cf.Session()
        if _socks_proxy:
            sess.proxies = {"http": _socks_proxy, "https": _socks_proxy}
        for c in _cj:
            sess.cookies.set(c.name, c.value, domain=c.domain or ".behance.net")
        _cf_session = sess
        _cf_impersonate = "firefox"
        _http_backend = "curl_cffi"
        log("HTTP via curl_cffi (Firefox impersonation)")
        return
    except ImportError:
        pass

    if shutil.which("curl"):
        _http_backend = "curl"
        log("HTTP via curl (HTTP/2)")
        return

    _opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_context()),
        urllib.request.HTTPCookieProcessor(_cj),
    )
    _opener.addheaders = list(HTML_HEADERS)
    _http_backend = "urllib"
    log("HTTP via urllib (TLS 1.3 Firefox ciphers)")


def _sync_cf_cookies():
    """Keep only allowlisted cookies from the response — tracking Set-Cookie
    values are what push a later request over the 431 header limit."""
    if _cf_session is None:
        return
    items = []
    try:
        items = [(c.name, c.value, getattr(c, "domain", None) or ".behance.net")
                 for c in _cf_session.cookies.jar]
    except Exception:  # noqa: BLE001
        items = [(n, v, ".behance.net") for n, v in _cf_session.cookies.items()]
    for name, value, domain in items:
        if name in COOKIE_KEEP:
            _add_cookie(name, value, domain)


def _header_map(raw_headers):
    out = {}
    if not raw_headers:
        return out
    if hasattr(raw_headers, "items"):
        for k, v in raw_headers.items():
            out[k.lower()] = v
        return out
    return {str(k).lower(): v for k, v in raw_headers}


def _raw_request(method, url, data=None, extra_headers=None):
    if _http_backend == "curl_cffi":
        return _cf_request(method, url, data, extra_headers)
    if _http_backend == "curl":
        return _curl_request(method, url, data, extra_headers)
    return _urllib_request(method, url, data, extra_headers)


def _cf_extras(kind):
    """Headers curl_cffi should add on top of the Firefox impersonation set.

    Impersonate already sends UA/Accept/Sec-Fetch/Cookie. Duplicating those
    plus a 21-cookie Adobe dump is what produced HTTP 431.
    """
    if kind == "gql":
        return (
            ("Origin", ROOT),
            ("Referer", ROOT + "/"),
            ("X-Requested-With", "XMLHttpRequest"),
            ("Content-Type", "application/json"),
            ("X-BCP", _bcp()),
        )
    return (("Referer", ROOT + "/"),)


def _cf_request(method, url, data, extra_headers):
    global _cf_impersonate
    # Never send Cookie here — the session jar already has it. A second
    # Cookie header doubles the size and Varnish answers 431.
    skip = {"cookie", "user-agent", "accept", "accept-language",
            "accept-encoding", "sec-fetch-dest", "sec-fetch-mode",
            "sec-fetch-site", "connection", "te"}
    headers = {k: v for k, v in (extra_headers or ()) if v
               and k.lower() not in skip}
    if url.startswith(GRAPHQL):
        headers.setdefault("X-BCP", _bcp())
    kwargs = {"headers": headers, "timeout": 60}
    if data is not None:
        kwargs["data"] = data
    fn = _cf_session.post if method == "POST" else _cf_session.get
    last = None
    seen = []
    for target in (_cf_impersonate, "firefox", "firefox133",
                   "chrome131", "chrome120", "chrome"):
        if not target or target in seen:
            continue
        seen.append(target)
        try:
            r = fn(url, impersonate=target, **kwargs)
            if target != _cf_impersonate:
                _cf_impersonate = target
                log(f"  (impersonating {target})")
            _sync_cf_cookies()
            return r.status_code, _header_map(r.headers), r.content
        except Exception as e:  # noqa: BLE001
            err = str(e).lower()
            if "impersonat" in err or "not supported" in err:
                last = e
                continue
            raise
    raise last or RuntimeError("curl_cffi impersonation failed")


def _curl_request(method, url, data, extra_headers):
    cookie_file = _write_cookie_file()
    fd, body_path = tempfile.mkstemp(prefix="behance_body_")
    os.close(fd)
    fd, hdr_path = tempfile.mkstemp(prefix="behance_hdr_")
    os.close(fd)
    cmd = [
        "curl", "-sS", "-L", "--http2", "--compressed",
        "--max-time", "60",
        "-A", UA,
        "-o", body_path,
        "-D", hdr_path,
        "-b", cookie_file,
        "-c", cookie_file,
        "-w", "%{http_code}",
        "-X", method,
    ]
    if _socks_proxy:
        parts = urllib.parse.urlparse(_socks_proxy)
        cmd += ["--socks5-hostname",
                f"{parts.hostname}:{parts.port or 1080}"]
    headers = list(extra_headers or HTML_HEADERS)
    if url.startswith(GRAPHQL):
        headers = list(headers) + [("X-BCP", _bcp())]
    headers = [(k, v) for k, v in headers if k.lower() != "cookie"]
    for k, v in headers:
        if v:
            cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["--data-binary", data.decode("utf-8") if isinstance(data, bytes) else data]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        try:
            code = int((proc.stdout or "0").strip() or "0")
        except ValueError:
            code = 0
        body = b""
        try:
            with open(body_path, "rb") as fh:
                body = fh.read()
        except OSError:
            pass
        raw_hdr = ""
        try:
            with open(hdr_path, "r", encoding="utf-8", errors="ignore") as fh:
                raw_hdr = fh.read()
        except OSError:
            pass
        headers = {}
        for line in raw_hdr.splitlines():
            if ":" in line and not line.lower().startswith("http/"):
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        _reload_cookie_file()
        if proc.returncode != 0 and code == 0:
            err = (proc.stderr or "").strip() or f"curl exit {proc.returncode}"
            raise urllib.error.URLError(err)
        return code, headers, body
    finally:
        for p in (body_path, hdr_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _urllib_request(method, url, data, extra_headers):
    headers = {k: v for k, v in (extra_headers or HTML_HEADERS) if v}
    headers["Cookie"] = _cookie_header()
    if url.startswith(GRAPHQL):
        headers["X-BCP"] = _bcp()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=60) as r:
            return r.status, _header_map(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, _header_map(e.headers), e.read()


def _retry_delay(attempt, headers):
    delay = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))
    ra = (headers or {}).get("retry-after")
    if ra:
        try:
            delay = max(delay, min(RETRY_AFTER_CAP, int(ra)))
        except ValueError:
            try:
                when = parsedate_to_datetime(ra)
                secs = int((when - datetime.now(timezone.utc)).total_seconds())
                delay = max(delay, min(RETRY_AFTER_CAP, max(secs, 0)))
            except (TypeError, ValueError, OverflowError):
                pass
    return delay


def _request(method, url, data=None, extra_headers=None, tries=DEFAULT_TRIES):
    """HTTP request with JS-challenge solving and 429 backoff."""
    last = None
    challenges = 0
    attempt = 0
    while attempt < tries:
        try:
            code, headers, body = _raw_request(method, url, data, extra_headers)
        except urllib.error.URLError as e:
            last = e
            attempt += 1
            if attempt >= tries:
                break
            time.sleep(min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt)))
            continue

        text = body.decode("utf-8", "ignore")
        if code == 403:
            m = re.search(r"js_challenge_value=([a-f0-9]+)", text)
            if m and challenges < 3:
                _set_challenge(m.group(1))
                if _cf_session is not None:
                    _cf_session.cookies.set(
                        "js_challenge_value", m.group(1), domain=".behance.net")
                challenges += 1
                continue
        if 200 <= code < 300:
            return code, headers, body
        if code == 431:
            last = RuntimeError("HTTP 431: request headers too large")
            if _trim_cookies(aggressive=True):
                log("  … HTTP 431 (headers too large); trimmed cookies, retrying")
                continue
            raise last
        last = RuntimeError(f"HTTP {code}: {text[:160]}")
        if code in (429, 500, 502, 503, 504):
            delay = _retry_delay(attempt, headers)
            hits = headers.get("x-last-60s-hits", "?")
            attempt += 1
            if attempt >= tries:
                break
            log(f"  … HTTP {code}; waiting {delay}s "
                f"(attempt {attempt}/{tries}, 60s-hits={hits})")
            time.sleep(delay)
            continue
        raise last
    raise last or RuntimeError(f"failed to fetch {url}")


def _headers_for(kind):
    if _http_backend == "curl_cffi":
        return _cf_extras(kind)
    return GQL_HEADERS if kind == "gql" else HTML_HEADERS


def fetch(url, tries=DEFAULT_TRIES):
    """GET a URL as text, transparently solving the JS cookie challenge."""
    _code, _headers, body = _request(
        "GET", url, extra_headers=_headers_for("html"), tries=tries)
    return body.decode("utf-8", "ignore")


def fetch_bytes(url, tries=3):
    _code, _headers, body = _request(
        "GET", url, extra_headers=_headers_for("html"), tries=tries)
    return body


def graphql(query, variables, tries=DEFAULT_TRIES):
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    _code, _headers, body = _request(
        "POST", GRAPHQL, data=payload, extra_headers=_headers_for("gql"),
        tries=tries)
    data = json.loads(body.decode("utf-8", "ignore") or "{}")
    if data.get("errors"):
        msgs = "; ".join(
            e.get("message", str(e)) for e in data["errors"][:3])
        raise RuntimeError(f"graphql: {msgs}")
    return data.get("data") or {}


def _prime_session():
    """Hit the homepage once so Varnish issues gk_suid + the JS challenge."""
    try:
        fetch(ROOT + "/", tries=4)
        log("Session primed")
    except Exception as e:  # noqa: BLE001
        log(f"! homepage warmup failed ({e}); continuing")


def _normalize_project(project):
    if not project:
        return None
    mods = project.get("modules")
    if project.get("allModules") and not (
            isinstance(mods, list) and mods):
        project["modules"] = project["allModules"]
    elif isinstance(mods, dict):
        project["modules"] = mods.get("nodes") or []
        project["_page_info"] = mods.get("pageInfo") or {}
    return project


def _gallery_via_graphql(gid):
    last_err = None
    for i, query in enumerate(GQL_PROJECT_QUERIES):
        try:
            data = graphql(query, {"id": int(gid)}, tries=DEFAULT_TRIES)
            project = _normalize_project((data or {}).get("project"))
            if not project:
                last_err = RuntimeError("graphql returned no project")
                continue
            # Paginate the connection form if needed.
            info = project.pop("_page_info", None)
            if info and info.get("hasNextPage") and info.get("endCursor"):
                after = info["endCursor"]
                # Only the second query supports `after`.
                page_q = GQL_PROJECT_QUERIES[-1]
                while after:
                    more = graphql(
                        page_q, {"id": int(gid), "after": after}, tries=4)
                    chunk = _normalize_project((more or {}).get("project"))
                    if not chunk:
                        break
                    project["modules"].extend(chunk.get("modules") or [])
                    nxt = chunk.pop("_page_info", {}) or {}
                    after = nxt.get("endCursor") if nxt.get("hasNextPage") else None
            return project
        except Exception as e:  # noqa: BLE001
            last_err = e
            # Schema mismatch — try the next query shape without burning retries.
            if "graphql:" in str(e) and i + 1 < len(GQL_PROJECT_QUERIES):
                continue
            raise
    if last_err:
        raise last_err
    return None


def gallery_data(gid):
    try:
        project = _gallery_via_graphql(gid)
        imgs = images_for(project) if project else []
        access = (project or {}).get("matureAccess")
        if project and (imgs or (access and access != "allowed")):
            log("  (via graphql)")
            return project
        if project:
            log("  … graphql returned no image URLs; falling back to HTML")
    except Exception as e:  # noqa: BLE001
        if "HTTP 429" in str(e) or "HTTP 431" in str(e):
            # 429: same IP bucket. 431: headers already trimmed; HTML won't help.
            raise
        log(f"  … graphql unavailable ({e}); falling back to HTML")

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
        # Prefer the `source` (or max_3840) path segment when present — that is
        # the original upload. Fall back to widest pixel width.
        by_name = {}
        for item in avail:
            url = item.get("url") or ""
            parts = url.rstrip("/").split("/")
            if len(parts) >= 2:
                by_name[parts[-2]] = item
        for key in ("source", "max_3840", "fs", "hd", "disp"):
            if key in by_name and by_name[key].get("url"):
                return by_name[key]["url"]
        best = max(avail, key=lambda x: x.get("width") or 0)
        if best.get("url"):
            return best["url"]
    for key in ("size_original", "size_max_3840", "size_1400",
                "size_max_1200", "size_disp"):
        entry = sizes.get(key)
        if isinstance(entry, dict) and entry.get("url"):
            return entry["url"]
    return module.get("src")


def _load_cookie_header(raw):
    """Load a raw 'name=value; name=value' Cookie header string."""
    n = 0
    for part in raw.strip().rstrip(";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            _add_cookie(name, value.strip())
            n += 1
    log(f"Loaded {n} cookie(s) from Cookie header string")
    return n


def _load_cookies(path):
    """Load cookies from a Netscape file, JSON export, or Cookie header."""
    raw = ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            raw = fh.read()
    except OSError as e:
        log(f"! could not read cookies file: {e}")
        return 0

    stripped = raw.lstrip()

    if (not stripped.startswith(("[", "{"))
            and "\t" not in raw
            and "# Netscape" not in raw
            and "=" in raw):
        return _load_cookie_header(raw)

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
        typename = mod.get("__typename") or ""
        if typename[:-6].lower() != "image":
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
    for attempt in range(3):
        try:
            blob = fetch_bytes(url, tries=2)
            if not blob:
                raise RuntimeError("empty body")
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, dest)
            log(f"    + {os.path.basename(dest)} ({len(blob) / 1048576:.1f} MB)")
            return True
        except Exception as e:  # noqa: BLE001
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
        o.get("display_name") or o.get("displayName") or ""
        for o in data.get("owners") or []
    ).strip(", ")
    log(f"  # {gid} — {title}" + (f" ({owners})" if owners else ""))

    imgs = images_for(data, module_id)
    if not imgs:
        access = data.get("matureAccess")
        if access and access != "allowed":
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
    """Collect gallery ids from a profile page (GraphQL, HTML fallback)."""
    query = """
    query GetProfileProjects($username: String, $after: String) {
      user(username: $username) {
        profileProjects(first: 12, after: $after) {
          pageInfo { endCursor hasNextPage }
          nodes { id }
        }
      }
    }
    """
    ids, seen = [], set()
    try:
        after = "MAo="  # "0" in base64 — gallery-dl's first page cursor
        while True:
            data = graphql(query, {"username": username, "after": after}, tries=6)
            block = ((data or {}).get("user") or {}).get("profileProjects") or {}
            for node in block.get("nodes") or []:
                gid = str(node.get("id") or "")
                if gid and gid not in seen:
                    seen.add(gid)
                    ids.append(gid)
            info = block.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")
            if not after:
                break
            if limit and len(ids) >= limit:
                break
        if ids:
            log("  (profile via graphql)")
            return ids[:limit] if limit else ids
    except Exception as e:  # noqa: BLE001
        log(f"  … profile graphql unavailable ({e}); falling back to HTML")

    page = fetch(f"{ROOT}/{username}")
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
        if not os.path.exists(args.cookies):
            log(f"! cookies file not found: {args.cookies}")
            return 2
        n = _load_cookies(args.cookies)
        if not n:
            return 2
        names = {c.name for c in _cj}
        if not names & {"sso_sid", "sso_uid", "iat0", "adobe_sso",
                        "bcp-sid", "sid", "user_sid"}:
            log("! warning: cookies loaded but no Behance session cookie found; "
                "mature galleries will still be skipped. Export cookies for "
                "behance.net while logged in.")
        log(f"Cookie header {len(_cookie_header())} B across "
            f"{sum(1 for _ in _cj)} cookie(s)")

    _init_http()
    _prime_session()

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
                time.sleep(5)
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
