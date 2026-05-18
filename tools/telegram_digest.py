"""Cron-style Telegram digest.

Builds today's daily snapshot, optionally asks Vertex AI for a 2-3 sentence
narrative (gemini-3.1-pro at the `reasoning` tier), formats for Telegram,
and sends via the configured bot.

Skips silently if no token / chat-id is set (so the systemd timer doesn't
spam errors on a paper-only setup).

Usage:
    python -m tools.telegram_digest                       # send today's digest
    python -m tools.telegram_digest --window-hours 168    # weekly digest
    python -m tools.telegram_digest --dry-run             # print, don't send
    python -m tools.telegram_digest --no-narrative        # skip the LLM call
"""
from __future__ import annotations

import argparse
import logging
import sys

from comms.telegram_digest import build_narrative, format_digest, send_digest
from config.settings import get_settings
from models.llm_client import NullLLM, build_llm_client
from record.research_log import ResearchLog
from record.track_record import TrackRecord
from tools.daily_snapshot import build_snapshot

log = logging.getLogger("alphagrid.digest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the formatted digest, don't send")
    parser.add_argument("--no-narrative", action="store_true",
                        help="skip the LLM narrative even when Vertex is configured")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level)

    settings = get_settings()
    tg = settings.telegram
    snap = build_snapshot(TrackRecord(), ResearchLog(), window_hours=args.window_hours)

    llm = NullLLM() if args.no_narrative else build_llm_client()
    narrative = build_narrative(snap, llm)

    if args.dry_run or not tg.telegram_bot_token or not tg.telegram_chat_id:
        if not args.dry_run:
            log.info("telegram bot token / chat id not configured; skipping send")
        print(format_digest(snap, narrative=narrative))
        return 0

    from comms.telegram_bot import PythonTelegramBotTransport
    transport = PythonTelegramBotTransport(tg.telegram_bot_token, int(tg.telegram_chat_id))
    sent = send_digest(snap, transport, llm=llm)
    print(sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
