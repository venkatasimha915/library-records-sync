"""Run a sync from the command line, e.g. from cron:

    0 * * * *  cd /opt/library-records-sync && python -m app.cli sync

Exit code is 0 on success, 1 if the run was partial or failed, so cron/monitoring can alert on it.
"""

import argparse
import logging
import sys

from app.api import source_from_settings
from app.config import get_settings
from app.db import init_db, make_engine, make_session_factory
from app.sync import SyncAlreadyRunning, run_sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="library-records-sync")
    sub = parser.add_subparsers(dest="command", required=True)
    sync_cmd = sub.add_parser("sync", help="Harvest records from the configured OAI-PMH source")
    sync_cmd.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)

    try:
        run = run_sync(make_session_factory(engine), source_from_settings(settings), trigger="cli",
                       max_pages=args.max_pages)
    except SyncAlreadyRunning:
        print("A sync is already running", file=sys.stderr)
        return 1

    print(
        f"run {run.id}: {run.status} | pages={run.pages} inserted={run.inserted} updated={run.updated} "
        f"unchanged={run.unchanged} deleted={run.deleted} failed={run.failed}"
        + (f" | error: {run.error}" if run.error else "")
    )
    return 0 if run.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
