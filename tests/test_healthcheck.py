"""Tests for the healthcheck core logic.

Network-dependent checks (Binance WS, Google News RSS, FinBERT load) are
not exercised here — we test the orchestration layer (run_check,
HealthReport, exit code) and the offline path.
"""
from __future__ import annotations

from tools.healthcheck import (
    CheckResult,
    HealthReport,
    check_database_roundtrip,
    check_risk_manager,
    check_settings,
    check_trade_router_paper,
    run_all,
    run_check,
)


# ---------------------------------------------------------------- run_check


def test_run_check_captures_pass() -> None:
    r = run_check("ok", lambda: ("PASS", "fine"))
    assert r.status == "PASS"
    assert r.detail == "fine"
    assert r.essential
    assert r.duration_ms >= 0


def test_run_check_catches_exceptions() -> None:
    def _boom():
        raise RuntimeError("kaboom")

    r = run_check("boom", _boom)
    assert r.status == "FAIL"
    assert "kaboom" in r.detail


def test_run_check_optional_flag() -> None:
    r = run_check("opt", lambda: ("SKIP", ""), essential=False)
    assert not r.essential
    assert r.status == "SKIP"


# ---------------------------------------------------------------- HealthReport


def test_report_exit_code_zero_on_optional_failures() -> None:
    rep = HealthReport()
    rep.add(CheckResult(name="e1", status="PASS"))
    rep.add(CheckResult(name="o1", status="FAIL", essential=False))
    assert rep.exit_code() == 0
    assert rep.optional_failures()
    assert not rep.essential_failures()


def test_report_exit_code_one_on_essential_failure() -> None:
    rep = HealthReport()
    rep.add(CheckResult(name="e1", status="FAIL"))
    rep.add(CheckResult(name="o1", status="PASS", essential=False))
    assert rep.exit_code() == 1
    assert rep.essential_failures()


def test_report_renders_human_summary() -> None:
    rep = HealthReport()
    rep.add(CheckResult(name="settings", status="PASS", detail="paper=true"))
    rep.add(CheckResult(name="binance ws", status="SKIP", essential=False))
    out = rep.render()
    assert "AlphaGrid health check" in out
    assert "settings" in out
    assert "binance ws" in out
    assert "All systems green" in out


def test_report_render_calls_out_essential_failures() -> None:
    rep = HealthReport()
    rep.add(CheckResult(name="db", status="FAIL", detail="locked"))
    out = rep.render()
    assert "ESSENTIAL FAILURES" in out
    assert "locked" in out


# ---------------------------------------------------------------- check functions


def test_settings_check_loads() -> None:
    status, detail = check_settings()
    assert status == "PASS"
    assert "paper=" in detail


def test_database_roundtrip_writes_and_reads() -> None:
    status, detail = check_database_roundtrip()
    assert status == "PASS"
    assert "sqlite" in detail


def test_risk_manager_check_runs() -> None:
    status, _ = check_risk_manager()
    assert status == "PASS"


def test_trade_router_check_executes_paper_trade() -> None:
    status, detail = check_trade_router_paper()
    assert status == "PASS"
    assert "trade_id=" in detail


# ---------------------------------------------------------------- orchestrator


def test_run_all_offline_skips_network_and_passes_essentials() -> None:
    rep = run_all(offline=True)
    # Network checks should be SKIP, not FAIL.
    network_names = {"finbert score", "google news rss", "news poller e2e", "binance ws + bar"}
    for r in rep.results:
        if r.name in network_names:
            assert r.status == "SKIP", f"{r.name} should skip offline, got {r.status}"
    # Essentials should pass.
    assert rep.exit_code() == 0
    assert not rep.essential_failures()
