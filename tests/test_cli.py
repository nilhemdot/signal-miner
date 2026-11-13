import pytest

from signal_miner.cli import main


def test_missing_dsn_exits_with_a_message(monkeypatch):
    monkeypatch.delenv("SM_DSN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["form-d-leads"])
    assert "SM_DSN" in str(exc.value)


def test_migrate_reports_nothing_pending(monkeypatch, db_dsn, capsys):
    monkeypatch.setenv("SM_ADMIN_DSN", db_dsn)
    assert main(["migrate"]) == 0
    assert capsys.readouterr().out == ""
