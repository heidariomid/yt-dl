#!/usr/bin/env python3
"""Download media from Telegram message links, including private t.me/c/ posts.

Needs a member session: TG_API_ID, TG_API_HASH, TG_SESSION.
Generate the session locally with scripts/telegram_session.py.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    FloodWaitError,
    SessionRevokedError,
)
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaWebPage, PeerChannel

PRIVATE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/c/(?P<chat>\d+)(?:/(?P<a>\d+))?(?:/(?P<b>\d+))?",
    re.I,
)
PUBLIC_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/(?:s/)?(?P<user>[A-Za-z]\w{3,})/(?P<msg>\d+)",
    re.I,
)
TG_PRIVATE_RE = re.compile(
    r"tg://privatepost\?channel=(?P<chat>\d+)&post=(?P<msg>\d+)"
    r"(?:&thread=(?P<topic>\d+))?",
    re.I,
)


def parse_link(url: str) -> dict:
    url = url.strip()
    m = TG_PRIVATE_RE.search(url)
    if m:
        return {
            "url": url,
            "chat": int(f"-100{m.group('chat')}"),
            "channel_id": int(m.group("chat")),
            "msg": int(m.group("msg")),
            "topic": int(m.group("topic")) if m.group("topic") else None,
        }
    m = PRIVATE_RE.search(url)
    if m:
        a, b = m.group("a"), m.group("b")
        if a and b:
            msg, topic = int(b), int(a)
        elif a:
            msg, topic = int(a), None
        else:
            raise ValueError(f"No message id in {url}")
        return {
            "url": url,
            "chat": int(f"-100{m.group('chat')}"),
            "channel_id": int(m.group("chat")),
            "msg": msg,
            "topic": topic,
        }
    m = PUBLIC_RE.search(url)
    if m:
        return {
            "url": url,
            "chat": m.group("user"),
            "channel_id": None,
            "msg": int(m.group("msg")),
            "topic": None,
        }
    raise ValueError(f"Unrecognized Telegram URL: {url}")


def split_urls(raw: str) -> list[str]:
    parts = re.split(r"[\s,]+", raw.strip())
    return [p for p in parts if p]


def safe_name(text: str, limit: int = 60) -> str:
    cleaned = re.sub(r"[^\w\s.-]+", "", text, flags=re.U).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return (cleaned or "telegram")[:limit]


def progress(current: int, total: int) -> None:
    if not total:
        print(f"\r  {current} bytes", end="", flush=True)
        return
    pct = current * 100 / total
    print(f"\r  {pct:5.1f}% ({current}/{total})", end="", flush=True)


async def resolve_entity(client: TelegramClient, parsed: dict):
    try:
        return await client.get_entity(parsed["chat"])
    except Exception as first:
        if parsed["channel_id"] is None:
            raise first
        print(f"get_entity failed ({first}); trying PeerChannel")
        return PeerChannel(parsed["channel_id"])


async def album_messages(client: TelegramClient, entity, msg) -> list:
    if not msg.grouped_id:
        return [msg]
    ids = list(range(max(1, msg.id - 12), msg.id + 13))
    around = await client.get_messages(entity, ids=ids)
    grouped = [m for m in around if m and m.grouped_id == msg.grouped_id]
    grouped.sort(key=lambda m: m.id)
    return grouped or [msg]


async def download_one(client: TelegramClient, parsed: dict, out_root: Path) -> Path:
    entity = await resolve_entity(client, parsed)
    msg = await client.get_messages(entity, ids=parsed["msg"])
    if not msg:
        raise RuntimeError(
            f"Message {parsed['msg']} not found in {parsed['chat']} "
            "(wrong id, or this session is not a member)"
        )

    chat_title = getattr(getattr(msg, "chat", None), "title", None) or str(parsed["chat"])
    folder = out_root / f"{safe_name(chat_title)}_{parsed['msg']}"
    folder.mkdir(parents=True, exist_ok=True)

    if msg.message:
        (folder / "caption.txt").write_text(msg.message, encoding="utf-8")

    messages = await album_messages(client, entity, msg)
    downloaded = []
    for m in messages:
        if not m.media or isinstance(m.media, MessageMediaWebPage):
            continue
        print(f"Downloading message {m.id}…")
        path = await client.download_media(m, file=str(folder) + "/", progress_callback=progress)
        print()
        if path:
            downloaded.append(path)
            print(f"  saved {path}")

    if not downloaded and not (folder / "caption.txt").exists():
        raise RuntimeError(f"Message {parsed['msg']} has no media and no text")
    if not downloaded:
        print(f"No media on {parsed['url']} — saved caption only")

    (folder / "source.txt").write_text(parsed["url"] + "\n", encoding="utf-8")
    return folder


async def run(urls: list[str], out_dir: Path, proxy) -> None:
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    session = os.environ.get("TG_SESSION", "").strip()
    if not api_id or not api_hash or not session:
        raise SystemExit(
            "Missing TG_API_ID / TG_API_HASH / TG_SESSION. "
            "Add them as repo secrets, then generate TG_SESSION locally with "
            "scripts/telegram_session.py"
        )

    client = TelegramClient(
        StringSession(session),
        int(api_id),
        api_hash,
        proxy=proxy,
        flood_sleep_threshold=120,
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SystemExit(
                "TG_SESSION is not authorized. Re-run scripts/telegram_session.py "
                "on a machine that can receive the Telegram login code."
            )

        me = await client.get_me()
        print(f"Logged in as {me.first_name} (id={me.id})")

        failed = 0
        for url in urls:
            parsed = parse_link(url)
            print(
                f"\n=== {url}\n    chat={parsed['chat']} "
                f"topic={parsed['topic']} msg={parsed['msg']}"
            )
            try:
                folder = await download_one(client, parsed, out_dir)
                print(f"OK {folder}")
            except ChannelPrivateError:
                failed += 1
                print(
                    f"FAIL {url}: this account is not a member of the private chat",
                    file=sys.stderr,
                )
            except FloodWaitError as e:
                failed += 1
                print(f"FAIL {url}: flood wait {e.seconds}s", file=sys.stderr)
            except Exception as e:
                failed += 1
                print(f"FAIL {url}: {e}", file=sys.stderr)

        if failed:
            raise SystemExit(f"{failed} URL(s) failed")
    except (AuthKeyUnregisteredError, SessionRevokedError):
        raise SystemExit(
            "Telegram rejected TG_SESSION (revoked or unregistered). "
            "Generate a new one with scripts/telegram_session.py"
        )
    finally:
        await client.disconnect()


def build_proxy():
    raw = os.environ.get("TG_PROXY") or os.environ.get("ALL_PROXY") or ""
    raw = raw.strip()
    if not raw:
        return None
    m = re.match(
        r"(?:socks5h?|socks5)://(?:[^@]+@)?(?P<host>[^:]+):(?P<port>\d+)",
        raw,
        re.I,
    )
    if not m:
        print(f"Ignoring unparsed TG_PROXY={raw}", file=sys.stderr)
        return None
    return {
        "proxy_type": "socks5",
        "addr": m.group("host"),
        "port": int(m.group("port")),
        "rdns": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Telegram message media")
    parser.add_argument("urls", nargs="+", help="t.me or t.me/c/ message links")
    parser.add_argument("-o", "--output", default="tmp_telegram", help="Output directory")
    args = parser.parse_args()

    urls = []
    for chunk in args.urls:
        urls.extend(split_urls(chunk))
    if not urls:
        raise SystemExit("No URLs given")

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(urls, out_dir, build_proxy()))


if __name__ == "__main__":
    main()
