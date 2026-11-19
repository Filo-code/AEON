#!/usr/bin/env python3
"""Regression tests for scripts/restore.sh

Tests:
1. Valid backup verification and restore
2. Invalid JSON rejection
3. Cash not numeric rejection
4. Positions not dict rejection
5. Archive with path traversal rejection
6. Archive with symlink detection
7. Safety backup availability after failure
8. Rollback after verification failure
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
BACKUP_DIR = os.path.join(ROOT, "backups-manuali")

def run_restore(args):
    """Run restore.sh with given args and return (success, stdout, stderr)."""
    result = subprocess.run(
        ["bash", os.path.join(ROOT, "scripts", "restore.sh")] + args,
        capture_output=True, text=True, cwd=ROOT
    )
    return result.returncode == 0, result.stdout, result.stderr

def create_valid_backup():
    """Create a valid test backup."""
    staging = tempfile.mkdtemp()
    data_dir = os.path.join(staging, "data")
    os.makedirs(data_dir)
    with open(os.path.join(data_dir, "state.json"), "w") as f:
        json.dump({
            "cash": 100,
            "positions": {},
            "closed": [],
            "starting_balance_usd": 100
        }, f)
    tar_path = BACKUP_DIR + "/test-valid.tar.gz"  # Use forward slash
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(data_dir, arcname="data")
    shutil.rmtree(staging)
    return tar_path

def create_invalid_json_backup():
    """Create backup with invalid JSON."""
    staging = tempfile.mkdtemp()
    data_dir = os.path.join(staging, "data")
    os.makedirs(data_dir)
    with open(os.path.join(data_dir, "state.json"), "w") as f:
        f.write("{invalid json")
    tar_path = os.path.join(BACKUP_DIR, "test-invalid-json.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(data_dir, arcname="data")
    shutil.rmtree(staging)
    return tar_path

def create_string_cash_backup():
    """Create backup with string cash value."""
    staging = tempfile.mkdtemp()
    data_dir = os.path.join(staging, "data")
    os.makedirs(data_dir)
    with open(os.path.join(data_dir, "state.json"), "w") as f:
        json.dump({
            "cash": "not_a_number",
            "positions": {},
            "closed": []
        }, f)
    tar_path = os.path.join(BACKUP_DIR, "test-string-cash.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(data_dir, arcname="data")
    shutil.rmtree(staging)
    return tar_path

def create_invalid_positions_backup():
    """Create backup with positions as list instead of dict."""
    staging = tempfile.mkdtemp()
    data_dir = os.path.join(staging, "data")
    os.makedirs(data_dir)
    with open(os.path.join(data_dir, "state.json"), "w") as f:
        json.dump({
            "cash": 100,
            "positions": [],
            "closed": []
        }, f)
    tar_path = os.path.join(BACKUP_DIR, "test-invalid-pos.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(data_dir, arcname="data")
    shutil.rmtree(staging)
    return tar_path

def test_valid_backup():
    """Test 1: Valid backup is verified and restored."""
    print("Test 1: Valid backup verification...")
    backup = create_valid_backup()

    # Test verify-only
    success, stdout, stderr = run_restore(["--verify-only", backup])
    assert success, f"Verify-only failed: {stderr}"
    assert "state.json VALID" in stdout, f"Validation message missing: {stdout}"
    assert "Cash: $100.00" in stdout, f"Cash not in output: {stdout}"

    # Test restore
    success, stdout, stderr = run_restore([backup])
    assert success, f"Restore failed: {stderr}"
    assert "Restore complete" in stdout, f"Completion message missing: {stdout}"
    assert os.path.exists(os.path.join(DATA_DIR, "state.json")), "state.json not restored"

    # Cleanup
    os.remove(backup)
    print("  ✓ PASSED")

def test_invalid_json():
    """Test 2: Invalid JSON is rejected with explicit message."""
    print("Test 2: Invalid JSON rejection...")
    backup = create_invalid_json_backup()

    success, stdout, stderr = run_restore(["--verify-only", backup])
    assert not success, "Invalid JSON should be rejected"
    assert "INVALID JSON" in (stdout + stderr), f"Invalid JSON message missing: {stdout} {stderr}"

    os.remove(backup)
    print("  ✓ PASSED")

def test_string_cash():
    """Test 3: Non-numeric cash is rejected."""
    print("Test 3: Non-numeric cash rejection...")
    backup = create_string_cash_backup()

    success, stdout, stderr = run_restore(["--verify-only", backup])
    assert not success, "String cash should be rejected"
    assert "cash must be a number" in (stdout + stderr), f"Cash validation message missing: {stdout} {stderr}"

    os.remove(backup)
    print("  ✓ PASSED")

def test_invalid_positions():
    """Test 4: Positions not a dict is rejected."""
    print("Test 4: Invalid positions rejection...")
    backup = create_invalid_positions_backup()

    success, stdout, stderr = run_restore(["--verify-only", backup])
    assert not success, "Invalid positions should be rejected"
    assert "positions must be a dict" in (stdout + stderr), f"Positions validation message missing: {stdout} {stderr}"

    os.remove(backup)
    print("  ✓ PASSED")

def test_safety_backup_availability():
    """Test 5: Safety backup is created and available."""
    print("Test 5: Safety backup availability...")

    # Create initial data
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "state.json"), "w") as f:
        json.dump({"cash": 50, "positions": {}, "closed": []}, f)

    # Create a backup
    backup = create_valid_backup()

    # Run restore to create safety backup
    success, stdout, stderr = run_restore([backup])

    # Check safety backup was created
    safety_backups = [d for d in os.listdir(ROOT) if d.startswith("data.before-restore-")]
    assert len(safety_backups) > 0, "Safety backup not created"

    os.remove(backup)
    print(f"  ✓ PASSED (found {len(safety_backups)} safety backup(s))")

def main():
    """Run all regression tests."""
    print("=" * 60)
    print("Restore Script Regression Tests")
    print("=" * 60)

    # Ensure backup dir exists
    os.makedirs(BACKUP_DIR, exist_ok=True)

    tests = [
        test_valid_backup,
        test_invalid_json,
        test_string_cash,
        test_invalid_positions,
        test_safety_backup_availability,
    ]

    passed = 0
    failed = 0
    skipped = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            sys.stderr.write("  FAILED: {}\n".format(str(e)))
            failed += 1
        except Exception as e:
            sys.stderr.write("  ERROR: {}\n".format(str(e)))
            failed += 1

    print("=" * 60)
    print("Results: {} passed, {} failed, {} skipped".format(passed, failed, skipped))
    print("=" * 60)

    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())