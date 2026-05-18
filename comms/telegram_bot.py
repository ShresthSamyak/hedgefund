"""Telegram approval gate.

Two layers, intentionally separated so the gate logic is testable without
hitting Telegram:

  * `TelegramApprovalGate` — the gate. Send-and-wait state machine that
    knows nothing about HTTP, only about a `TelegramTransport` it can
    `send(text)` to and a reply callback it can register.
  * `PythonTelegramBotTransport` — the adapter that runs an asyncio
    `Application` from python-telegram-bot v21 in a daemon thread and
    bridges its events to the gate.

Reply format:
    `YES <request_id>`  -> approves
    `NO  <request_id>`  -> rejects
    anything else       -> ignored
    no reply within timeout -> timed_out (auto-reject by the router)
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Protocol

from comms.approval_gate import (
    ApprovalGate,
    ApprovalOutcome,
    ApprovalRequest,
    format_proposal_message,
)

log = logging.getLogger(__name__)

ReplyCallback = Callable[[str], None]


class TelegramTransport(Protocol):
    def send(self, text: str) -> None: ...
    def register_reply_handler(self, cb: ReplyCallback) -> None: ...


class TelegramApprovalGate(ApprovalGate):
    """Send a formatted proposal, block until a reply with the matching id arrives."""

    def __init__(self, transport: TelegramTransport) -> None:
        self._transport = transport
        self._pending: dict[str, threading.Event] = {}
        self._outcomes: dict[str, ApprovalOutcome] = {}
        self._lock = threading.Lock()
        transport.register_reply_handler(self._on_reply)

    def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        event = threading.Event()
        with self._lock:
            self._pending[req.request_id] = event

        try:
            self._transport.send(format_proposal_message(req))
        except Exception as exc:
            log.exception("telegram send failed for %s", req.request_id)
            with self._lock:
                self._pending.pop(req.request_id, None)
            return ApprovalOutcome(
                state="rejected",
                request_id=req.request_id,
                note=f"transport error: {exc!r}",
            )

        responded = event.wait(timeout=req.timeout.total_seconds())
        with self._lock:
            self._pending.pop(req.request_id, None)
            if responded:
                outcome = self._outcomes.pop(req.request_id, None)
                if outcome is not None:
                    return outcome
        return ApprovalOutcome(
            state="timed_out",
            request_id=req.request_id,
            note=f"no reply within {req.timeout.total_seconds():.0f}s",
        )

    def _on_reply(self, text: str) -> None:
        verb, rid = _parse_reply(text)
        if verb is None or rid is None:
            return
        state = "approved" if verb == "YES" else "rejected"
        with self._lock:
            event = self._pending.get(rid)
            if event is None:
                log.info("ignored reply for unknown/expired request %s", rid)
                return
            self._outcomes[rid] = ApprovalOutcome(state=state, request_id=rid, note=f"user said {verb}")
            event.set()


def _parse_reply(text: str) -> tuple[str | None, str | None]:
    parts = text.strip().split()
    if len(parts) < 2:
        return None, None
    verb = parts[0].upper()
    if verb not in {"YES", "NO"}:
        return None, None
    return verb, parts[1]


class PythonTelegramBotTransport:
    """Adapter for python-telegram-bot v21.

    Runs an `Application` inside a daemon thread with its own asyncio loop.
    Bridges incoming text messages from the configured chat to the gate's
    reply handler.

    Constructed lazily so this module imports cleanly even if
    `python-telegram-bot` is not installed.
    """

    def __init__(self, bot_token: str, chat_id: int, *, startup_timeout: float = 15.0) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")
        if not chat_id:
            raise ValueError("chat_id is required")
        self._token = bot_token
        self._chat_id = chat_id
        self._reply_cb: ReplyCallback | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="telegram-bot")
        self._thread.start()
        if not self._ready.wait(timeout=startup_timeout):
            raise RuntimeError("Telegram bot did not start within timeout")
        if self._start_error is not None:
            raise self._start_error

    def register_reply_handler(self, cb: ReplyCallback) -> None:
        self._reply_cb = cb

    def send(self, text: str) -> None:
        if self._loop is None or self._app is None:
            raise RuntimeError("Telegram transport not initialised")
        fut = asyncio.run_coroutine_threadsafe(
            self._app.bot.send_message(chat_id=self._chat_id, text=text),
            self._loop,
        )
        fut.result(timeout=30)

    def _run(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            from telegram.ext import Application, MessageHandler, filters

            self._app = (
                Application.builder().token(self._token).build()
            )
            self._app.add_handler(
                MessageHandler(
                    filters.TEXT & filters.Chat(self._chat_id),
                    self._handle_message,
                )
            )
            self._loop.run_until_complete(self._app.initialize())
            self._loop.run_until_complete(self._app.updater.start_polling())
            self._loop.run_until_complete(self._app.start())
            self._ready.set()
            self._loop.run_forever()
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            raise

    async def _handle_message(self, update, _context) -> None:
        if self._reply_cb is None:
            return
        if update.message and update.message.text:
            try:
                self._reply_cb(update.message.text)
            except Exception:
                log.exception("reply handler raised")
