#!/usr/bin/env python3
"""
PGMRec — restore_data.py

Restores database, channel configs, manifests, and .env from a backup ZIP
produced by backup_data.py.

Usage:
    python scripts/restore_data.py pgmrec-backup-20250101_120000.zip
    python scripts/restore_data.py backup.zip --target-dir /opt/pgmrec

⚠ WARNING: By default this will OVERWRITE the existing database and configs.
           Stop PGMRec before restoring to avoid data corruption.
           Pass --dry-run to preview what would be restored.

Cross-platform: runs on Linux and Windows.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

# ── Default paths (relative to repository root) ───────────────────────────────
_REPO_ROOT = Path(__file__).parent.parent.resolve()
_BACKEND_DIR = _REPO_ROOT / "backend"


def restore(
    archive: Path,
    target_backend: Path,
    target_root: Path,
    dry_run: bool,
) -> None:
    if not archive.exists():
        print(f"ERROR: Archive not found: {archive}", file=sys.stderr)
        sys.exit(1)

    print(f"Restoring from: {archive}")
    if dry_run:
        print("DRY RUN — no files will be written.\n")

    with zipfile.ZipFile(archive, "r") as zf:
        names = zf.namelist()

        for name in sorted(names):
            if name.endswith("/"):
                continue  # skip directory entries

            # Map archive member to destination
            if name == "pgmrec.db":
                dest = target_backend / "pgmrec.db"
            elif name == ".env":
                dest = target_root / ".env"
            elif name.startswith("data/"):
                dest = target_backend / name
            else:
                print(f"  ? Unknown entry '{name}' — skipping.")
                continue

            print(f"  → {name}  =>  {dest}")

            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Write atomically: extract to temp, then rename
                tmp = dest.with_suffix(dest.suffix + ".restoring")
                with zf.open(name) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                tmp.replace(dest)

    if dry_run:
        print("\nDry run complete — nothing written.")
    else:
        print(f"\nRestore complete.  Start PGMRec to apply.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PGMRec data restore tool")
    p.add_argument(
        "archive",
        type=Path,
        help="Path to backup ZIP produced by backup_data.py",
    )
    p.add_argument(
        "--target-dir",
        type=Path,
        default=_BACKEND_DIR,
        help=f"Backend directory to restore into  (default: {_BACKEND_DIR})",
    )
    p.add_argument(
        "--root-dir",
        type=Path,
        default=_REPO_ROOT,
        help=f"Repo/install root for .env  (default: {_REPO_ROOT})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be restored without writing anything",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    restore(
        archive=args.archive,
        target_backend=args.target_dir,
        target_root=args.root_dir,
        dry_run=args.dry_run,
    )
