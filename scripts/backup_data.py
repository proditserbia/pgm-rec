#!/usr/bin/env python3
"""
PGMRec — backup_data.py

Creates a timestamped ZIP archive containing the database, channel configs,
manifests, and app config (.env).  Large video recordings are excluded.

Usage:
    python scripts/backup_data.py
    python scripts/backup_data.py --output /mnt/backup/pgmrec-$(date +%Y%m%d).zip
    python scripts/backup_data.py --data-dir /opt/pgmrec/data --db /opt/pgmrec/pgmrec.db

Cross-platform: runs on Linux and Windows.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# ── Default paths (relative to repository root) ───────────────────────────────
_REPO_ROOT = Path(__file__).parent.parent.resolve()
_BACKEND_DIR = _REPO_ROOT / "backend"

_DEFAULT_DB = _BACKEND_DIR / "pgmrec.db"
_DEFAULT_DATA_DIR = _BACKEND_DIR / "data"
_DEFAULT_ENV = _REPO_ROOT / ".env"


def _add_dir(zf: zipfile.ZipFile, directory: Path, arcname_prefix: str) -> int:
    """Recursively add all files in *directory* to *zf* under *arcname_prefix*."""
    count = 0
    if not directory.exists():
        return count
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            arc = arcname_prefix + "/" + path.relative_to(directory).as_posix()
            zf.write(path, arc)
            count += 1
    return count


def backup(
    db: Path,
    data_dir: Path,
    env_file: Path | None,
    output: Path,
) -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if output.is_dir():
        output = output / f"pgmrec-backup-{timestamp}.zip"

    print(f"Backing up to: {output}")

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Database
        if db.exists():
            zf.write(db, "pgmrec.db")
            print(f"  + {db} → pgmrec.db")
        else:
            print(f"  ⚠ Database not found: {db}", file=sys.stderr)

        # Channel configs
        channels_dir = data_dir / "channels"
        n = _add_dir(zf, channels_dir, "data/channels")
        print(f"  + data/channels/ ({n} files)")

        # Manifests (JSON index, not video)
        manifests_dir = data_dir / "manifests"
        n = _add_dir(zf, manifests_dir, "data/manifests")
        print(f"  + data/manifests/ ({n} files)")

        # .env (app config / secrets)
        if env_file and env_file.exists():
            zf.write(env_file, ".env")
            print(f"  + {env_file} → .env")

    size_mb = output.stat().st_size / 1_048_576
    print(f"\nBackup complete: {output}  ({size_mb:.1f} MB)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PGMRec data backup tool")
    p.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB,
        help=f"Path to pgmrec.db  (default: {_DEFAULT_DB})",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=_DEFAULT_DATA_DIR,
        help=f"Path to data directory  (default: {_DEFAULT_DATA_DIR})",
    )
    p.add_argument(
        "--env",
        type=Path,
        default=_DEFAULT_ENV if _DEFAULT_ENV.exists() else None,
        help="Path to .env file  (omit to skip)",
    )
    p.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("."),
        help="Output ZIP file or directory  (default: current dir, auto-named)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    backup(
        db=args.db,
        data_dir=args.data_dir,
        env_file=args.env,
        output=args.output,
    )
