"""Pub/sub signal bus.

The bus is how research agents shout findings to trading agents without
having to know who is listening. Two implementations:

  * `InMemoryBus` — used for tests, paper-mode burn-in, and any single-process
    deployment. Zero infra.
  * `RedisBus` — used in production when multiple processes (or a separate
    dashboard) need to subscribe. Lazy-imports `redis`.

Channels we use today:
  * `price.<symbol>`        — every closed candle, payload = OHLCBar + meta
  * `news.alert`            — when a headline scores past a threshold
  * `news.raw`              — every dedup'd article (lower-rate stream)
  * `research.regime`       — when research_crypto updates portfolio regime
  * `research.size_modifier`— when trading_crypto_sent updates the gate

The bus is threadsafe — publish() from any thread is fine. Subscribers run
on the bus's internal dispatch thread, so callbacks should be quick and
non-blocking. Long work belongs on the agent's own queue.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)

SubscriberCallback = Callable[[str, Any], None]


class SignalBus(Protocol):
    def publish(self, channel: str, payload: Any) -> None: ...
    def subscribe(self, channel: str, callback: SubscriberCallback) -> None: ...
    def shutdown(self) -> None: ...


class InMemoryBus:
    """Single-process pub/sub. Dispatch runs on a daemon thread so publishers
    don't block on subscriber work.
    """

    def __init__(self, *, dispatch_in_thread: bool = True) -> None:
        self._subscribers: dict[str, list[SubscriberCallback]] = {}
        self._lock = threading.RLock()
        self._dispatch_in_thread = dispatch_in_thread
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._stop = threading.Event()
        if dispatch_in_thread:
            self._thread = threading.Thread(target=self._dispatch_loop, daemon=True, name="signal-bus")
            self._thread.start()
        else:
            self._thread = None

    def publish(self, channel: str, payload: Any) -> None:
        if self._dispatch_in_thread:
            self._queue.put((channel, payload))
        else:
            self._fanout(channel, payload)

    def subscribe(self, channel: str, callback: SubscriberCallback) -> None:
        with self._lock:
            self._subscribers.setdefault(channel, []).append(callback)

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=2.0)

    # -- testing helpers --------------------------------------------------

    def drain(self, timeout: float = 1.0) -> None:
        """Block until all currently-queued messages are dispatched. Tests
        call this so they don't race with the dispatch thread.
        """
        if not self._dispatch_in_thread:
            return
        marker = threading.Event()

        def _sentinel(_ch: str, _p: Any) -> None:
            marker.set()

        sentinel_channel = f"__drain_{id(marker)}"
        self.subscribe(sentinel_channel, _sentinel)
        self.publish(sentinel_channel, None)
        marker.wait(timeout)

    # -- internals --------------------------------------------------------

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                return
            channel, payload = item
            self._fanout(channel, payload)

    def _fanout(self, channel: str, payload: Any) -> None:
        with self._lock:
            subs = list(self._subscribers.get(channel, ()))
        for cb in subs:
            try:
                cb(channel, payload)
            except Exception:
                log.exception("signal_bus subscriber raised on channel %s", channel)


class RedisBus:
    """Production bus. Uses `redis` PUBSUB. Lazy-imports redis so the module
    loads without it.

    Each subscribe() spawns a dedicated daemon thread that runs the redis
    listen loop and dispatches into the user's callback. Payloads are
    JSON-serialized on publish and decoded on subscribe.
    """

    def __init__(self, url: str) -> None:
        try:
            import redis as redis_lib
        except ImportError as exc:
            raise RuntimeError("redis package required for RedisBus") from exc
        self._client: Any = redis_lib.from_url(url, decode_responses=True)
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def publish(self, channel: str, payload: Any) -> None:
        import json
        self._client.publish(channel, json.dumps(payload, default=str))

    def subscribe(self, channel: str, callback: SubscriberCallback) -> None:
        pubsub = self._client.pubsub()
        pubsub.subscribe(channel)

        def _loop() -> None:
            import json
            for msg in pubsub.listen():
                if self._stop.is_set():
                    return
                if msg.get("type") != "message":
                    continue
                try:
                    payload = json.loads(msg["data"])
                except (TypeError, ValueError):
                    payload = msg["data"]
                try:
                    callback(channel, payload)
                except Exception:
                    log.exception("redis subscriber raised on channel %s", channel)

        t = threading.Thread(target=_loop, daemon=True, name=f"redis-sub-{channel}")
        t.start()
        self._threads.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._client.close()
        except Exception:
            log.exception("redis client close failed")
