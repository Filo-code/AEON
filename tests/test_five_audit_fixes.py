"""Regression tests for AEON audit fixes (storage, backups, risk manager, wallet intelligence)."""
import json
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
import storage
from risk import RiskManager


def test_load_json_safe_all_files_missing_returns_missing(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    obj, source = storage.load_json_safe(
        str(data / "state.json"), backup_dir=str(data / "backups"), backup_name="state.json"
    )
    assert obj is None
    assert source == "missing"


def test_load_json_safe_main_corrupt_backup_absent_returns_corrupt(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "state.json").write_text("{corrupt_json", encoding="utf-8")
    obj, source = storage.load_json_safe(
        str(data / "state.json"), backup_dir=str(data / "backups"), backup_name="state.json"
    )
    assert obj is None
    assert source == "corrupt"


def test_load_json_safe_recovers_snapshot_when_main_and_bak_missing(tmp_path):
    data = tmp_path / "data"
    backups = data / "backups" / "20261117-010000"
    backups.mkdir(parents=True)
    (backups / "state.json").write_text('{"cash": 12}', encoding="utf-8")
    obj, source = storage.load_json_safe(
        str(data / "state.json"), backup_dir=str(data / "backups"), backup_name="state.json"
    )
    assert obj == {"cash": 12}
    assert source == "backup:20261117-010000"


def test_backup_rejects_corrupt_and_non_dict_auxiliary_json(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "state.json").write_text('{"ok": true}', encoding="utf-8")
    (data / "risk.json").write_text('{broken', encoding="utf-8")
    monkeypatch.setattr(storage, "disk_status", lambda _p: {"free_mb": 9999})
    result = storage.Backups(str(data), min_free_mb=1).run()
    assert result["ok"] is False
    assert not list((data / "backups").glob("20*"))

    (data / "risk.json").write_text('[]', encoding="utf-8")
    result_arr = storage.Backups(str(data), min_free_mb=1).run()
    assert result_arr["ok"] is False


def test_risk_manager_rejects_non_finite_and_bool_inputs():
    rm = RiskManager({"risk": {"mode": "conservative", "conservative": {"max_consecutive_losses": 2}}})
    rm.on_trade_closed(10.0, 110.0)
    assert rm.s["peak_equity"] == 110.0
    assert rm.s["consec_losses"] == 0

    rm.on_trade_closed(float("nan"), 110.0)
    rm.on_trade_closed(-5.0, float("inf"))
    rm.on_trade_closed(True, 110.0)
    assert rm.s["consec_losses"] == 0
    assert rm.s["peak_equity"] == 110.0
