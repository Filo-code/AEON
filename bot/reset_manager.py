"""Safe, auditable full paper-session reset with source/config and data backups."""
from __future__ import annotations
import os, tarfile, time, logging, shutil
from pathlib import Path

log = logging.getLogger("paperbot.reset")
EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules", "backups"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _add_project(tar, root: Path):
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts) or (rel.parts and rel.parts[0] == "data"):
            continue
        if p.is_file() and p.suffix not in EXCLUDE_SUFFIXES:
            tar.add(p, arcname=str(Path(root.name) / rel), recursive=False)


def create_reset_backups(project_root: str, data_dir: str) -> dict:
    root, data = Path(project_root).resolve(), Path(data_dir).resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    # Keep archives on the persistent DATA_DIR volume (Docker bind-mount and systemd safe).
    backup_root = data / "reset_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_root, 0o700)
    dest = backup_root / stamp
    if dest.exists():
        dest = backup_root / (stamp + f"-{time.time_ns() % 1_000_000:06d}")
    dest.mkdir(parents=True, exist_ok=False)
    os.chmod(dest, 0o700)
    project_tar = dest / "project-source-config.tar.gz"
    data_tar = dest / "paper-data.tar.gz"
    with tarfile.open(project_tar, "w:gz") as tf:
        _add_project(tf, root)
    with tarfile.open(data_tar, "w:gz") as tf:
        if data.exists():
            for p in data.rglob("*"):
                rel = p.relative_to(data)
                if any(part in EXCLUDE_DIRS or part == "reset_backups" for part in rel.parts):
                    continue
                if p.is_file() and p.suffix not in EXCLUDE_SUFFIXES:
                    tf.add(p, arcname=str(Path("data") / rel), recursive=False)
    os.chmod(project_tar, 0o600)
    os.chmod(data_tar, 0o600)
    return {"directory": str(dest), "project_backup": str(project_tar), "data_backup": str(data_tar),
            "project_bytes": project_tar.stat().st_size, "data_bytes": data_tar.stat().st_size}


def archive_and_clear_data(data_dir: str) -> str:
    """Clear the mounted data directory in place after a verified backup (Docker bind-mount safe)."""
    data = Path(data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    for child in list(data.iterdir()):
        if child.name in ("reset_backups", "telegram_offset.json"):
            continue  # preserve backups and Telegram update cursor to avoid replaying old commands
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
    # Guard against a legacy RESET_STATE value remaining in .env after the Telegram reset.
    reset_value = os.getenv("RESET_STATE", "").strip()
    if reset_value and reset_value.lower() not in ("0", "false", "no", "off"):
        try:
            from storage import atomic_json
            atomic_json(str(data / "reset.done"), {"value": reset_value, "ts": time.time(), "source": "telegram"})
        except Exception:
            log.warning("Could not persist reset.done guard for RESET_STATE")
    return str(data)
