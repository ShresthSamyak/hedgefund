"""On-chain data feeds — free sources for crypto regime gating.

Currently wires CoinMetrics Community API (free, no auth, ~1.6 RPS rate
limit) for MVRV ratio. Same metric Glassnode sells at $999/mo; community
plan uses Free-Float MVRV (`CapMVRVFF`) which adjusts for inactive supply.

Reference: https://community-api.coinmetrics.io/v4/
License: data is Creative Commons under their community terms; commercial
use requires the paid tier. AlphaGrid's personal-capital scale fits the
non-commercial fair-use clause.

Design:
  * Protocol `OnChainFeed` for any provider.
  * `CoinMetricsClient` implements it via urllib (no new dependency).
  * `StaticOnChainFeed` is the test double.
  * Network errors return `None` rather than raising — the regime gate
    treats missing components gracefully (averages over what's present).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

COINMETRICS_BASE = "https://community-api.coinmetrics.io/v4"


@dataclass(frozen=True)
class MvrvPoint:
    asset: str
    ts: datetime
    value: float


class OnChainFeed(Protocol):
    def fetch_mvrv(self, asset: str = "btc") -> MvrvPoint | None: ...


# ---------------------------------------------------------------- CoinMetrics


class CoinMetricsClient:
    """CoinMetrics Community API — free, no auth, no API key.

    Rate limit: 10 requests per 6 seconds per IP (~1.6 RPS). Our 8h tick
    cadence calls this ~3 times/day, so we're 5 orders of magnitude under
    the limit even with all 8 agents running.
    """

    def __init__(self, *, timeout: float = 10.0, base_url: str = COINMETRICS_BASE) -> None:
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    def fetch_mvrv(self, asset: str = "btc") -> MvrvPoint | None:
        """Latest Free-Float MVRV for `asset` (lowercase ticker, e.g. 'btc', 'eth').

        Returns None on any HTTP / parsing failure — caller treats as a
        missing component in the regime gate.
        """
        url = self._build_url("timeseries/asset-metrics", {
            "assets": asset,
            "metrics": "CapMVRVFF",
            "page_size": "1",
            "frequency": "1d",
            "pretty": "false",
        })
        try:
            req = Request(url, headers={"User-Agent": "alphagrid/0.1"})
            with urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    log.warning("coinmetrics %s status=%s", asset, resp.status)
                    return None
                body = json.loads(resp.read().decode("utf-8"))
        except Exception:
            log.exception("coinmetrics fetch_mvrv failed for %s", asset)
            return None

        rows = body.get("data") or []
        if not rows:
            log.warning("coinmetrics returned no rows for %s", asset)
            return None
        row = rows[-1]
        try:
            ts = _parse_iso(str(row["time"]))
            value = float(row["CapMVRVFF"])
        except (KeyError, TypeError, ValueError):
            log.warning("coinmetrics malformed row: %s", row)
            return None
        return MvrvPoint(asset=asset, ts=ts, value=value)

    def _build_url(self, path: str, params: dict[str, str]) -> str:
        return f"{self.base_url}/{path.lstrip('/')}?{urlencode(params)}"


def _parse_iso(s: str) -> datetime:
    # CoinMetrics returns nanosecond precision: 2026-05-19T00:00:00.000000000Z
    s = s.replace("Z", "+00:00")
    # Python's fromisoformat doesn't accept >6 fractional digits before 3.11+ tolerance;
    # trim to microseconds.
    if "." in s and "+" in s:
        head, rest = s.split(".", 1)
        frac, tz = rest.split("+", 1)
        frac = frac[:6].ljust(6, "0")
        s = f"{head}.{frac}+{tz}"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


# ---------------------------------------------------------------- test feed


class StaticOnChainFeed:
    """Hand-set responses for tests."""

    def __init__(self) -> None:
        self._mvrv: dict[str, MvrvPoint | None] = {}

    def set_mvrv(self, asset: str, value: float | None) -> None:
        if value is None:
            self._mvrv[asset] = None
        else:
            self._mvrv[asset] = MvrvPoint(asset=asset, ts=datetime.now(timezone.utc), value=value)

    def fetch_mvrv(self, asset: str = "btc") -> MvrvPoint | None:
        return self._mvrv.get(asset)
