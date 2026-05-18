"""Tests for the dry-run CLI."""
from __future__ import annotations

from pathlib import Path

from tools.dry_run import main


def test_dry_run_offline_completes_and_writes_db(tmp_path: Path, capsys) -> None:
    db = tmp_path / "dry.db"
    exit_code = main([
        "--days", "3",
        "--step-hours", "4",
        "--db", str(db),
        "--log-level", "ERROR",
    ])
    assert exit_code == 0
    assert db.exists() and db.stat().st_size > 0
    out = capsys.readouterr().out
    assert "Dry-run complete" in out
    assert "ALPHAGRID_DB_URL" in out
    assert "uvicorn api.main:app" in out


def test_dry_run_replaces_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "dry.db"
    db.write_text("not a real sqlite file")
    pre_size = db.stat().st_size
    main([
        "--days", "2", "--step-hours", "4",
        "--db", str(db), "--log-level", "ERROR",
    ])
    # File replaced, now actually a sqlite database.
    assert db.stat().st_size != pre_size


def test_dry_run_creates_parent_dir(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "deep" / "dry.db"
    assert not db.parent.exists()
    main([
        "--days", "2", "--step-hours", "4",
        "--db", str(db), "--log-level", "ERROR",
    ])
    assert db.exists()


def test_dry_run_summary_has_all_8_agent_invocations(tmp_path: Path, capsys) -> None:
    main([
        "--days", "2", "--step-hours", "4",
        "--db", str(tmp_path / "x.db"), "--log-level", "ERROR",
    ])
    out = capsys.readouterr().out
    for name in (
        "research_india", "research_crypto", "trading_funding",
        "trading_momentum", "trading_sentiment", "trading_pairs",
        "trading_trend", "trading_crypto_sent",
    ):
        assert name in out, f"missing {name} in summary"
