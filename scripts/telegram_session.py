#!/usr/bin/env python3
"""Create a Telethon session string for the Telegram workflow.

Run this on your own machine (it sends a login code to Telegram):

    pip3 install telethon
    export TG_API_ID=123456
    export TG_API_HASH=your_hash_from_my.telegram.org
    python3 scripts/telegram_session.py

Paste the printed string into the repo secret TG_SESSION.
The account must already be a member of the private chat.
"""

from __future__ import annotations

import os
import sys

from telethon.sessions import StringSession
from telethon.sync import TelegramClient


def main() -> None:
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    if not api_id or not api_hash:
        sys.exit("Set TG_API_ID and TG_API_HASH (from https://my.telegram.org)")

    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        session = client.session.save()
        me = client.get_me()
        print(f"Logged in as {me.first_name} (id={me.id})", file=sys.stderr)
        print(session)


if __name__ == "__main__":
    main()
