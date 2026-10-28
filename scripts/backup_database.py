"""Consistent online backup of the server database.

SQLite: uses SQLite's online backup API (safe while the server is running, WAL
included), verifies the copy with PRAGMA integrity_check and keeps the newest
N backups.  PostgreSQL: prints the pg_dump command to use instead.

    server\\.venv\\Scripts\\python scripts\\backup_database.py                  (-> backups\\)
    server\\.venv\\Scripts\\python scripts\\backup_database.py --dest D:\\backups --keep 60

Schedule it daily with Windows Task Scheduler (see docs/installation-windows.md).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Back up the temperature monitoring database")
    ap.add_argument("--dest", default=str(ROOT / "backups"))
    ap.add_argument("--keep", type=int, default=30, help="number of backups to keep")
    args = ap.parse_args()

    from app.config import Config  # loads server/.env via app package import

    url = Config.SQLALCHEMY_DATABASE_URI
    if not url.startswith("sqlite"):
        parsed = urlparse(url)
        print("PostgreSQL detected. Back up with:")
        print(f'  pg_dump -h {parsed.hostname} -p {parsed.port or 5432} -U {parsed.username} -Fc '
              f'-f backup-{datetime.now():%Y%m%d-%H%M}.dump {parsed.path.lstrip("/")}')
        return 0

    source = Path(url.replace("sqlite:///", "", 1))
    if not source.exists():
        print(f"database not found: {source}", file=sys.stderr)
        return 1
    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"temperature-{datetime.now():%Y%m%d-%H%M%S}.db"

    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst, pages=1024)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        print(f"integrity check FAILED on {target}: {result}", file=sys.stderr)
        return 2
    size_mb = target.stat().st_size / 1_048_576
    print(f"backup written: {target} ({size_mb:.1f} MB, integrity ok)")

    backups = sorted(dest_dir.glob("temperature-*.db"))
    for old in backups[:-args.keep] if args.keep > 0 else []:
        old.unlink()
        print(f"removed old backup {old.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
