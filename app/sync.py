"""The harvest: page through the source and bring our database in line with it.

Rules for each record on a page:
  new identifier                -> insert
  same identifier, same content -> unchanged (nothing written, so re-running is safe)
  same identifier, new content  -> update (and reactivate it if it had been deleted)
  header says status="deleted"  -> mark our copy deleted (we keep the row for history)
  can't be parsed / no title    -> saved to sync_errors with the reason; the rest of the page still imports

Each page is saved in its own database transaction. If page 3 of 10 fails to download,
pages 1-2 stay saved and the run is marked "partial" so someone can look at it.
"""

import logging
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Record, SyncError, SyncRun, utcnow
from app.normalize import InvalidRecord, normalize, to_db_columns
from app.oai import OAIFetchError, OAIProtocolError, OAISource, parse_page

log = logging.getLogger(__name__)

_run_lock = threading.Lock()


class SyncAlreadyRunning(Exception):
    pass


def run_sync(
    session_factory: sessionmaker,
    source: OAISource,
    trigger: str = "manual",
    max_pages: int | None = None,
) -> SyncRun:
    if not _run_lock.acquire(blocking=False):
        raise SyncAlreadyRunning("A sync is already running")
    try:
        return _run(session_factory, source, trigger, max_pages)
    finally:
        _run_lock.release()


def is_running() -> bool:
    return _run_lock.locked()


def _run(session_factory: sessionmaker, source: OAISource, trigger: str, max_pages: int | None) -> SyncRun:
    with session_factory() as session:
        run = SyncRun(trigger=trigger, status="running")
        session.add(run)
        session.commit()
        run_id = run.id

    token: str | None = None
    seen_tokens: set[str] = set()
    pages = 0
    error: str | None = None

    while True:
        if max_pages is not None and pages >= max_pages:
            break
        try:
            page = parse_page(source.fetch_page(token))
        except (OAIFetchError, OAIProtocolError) as exc:
            error = f"Page {pages + 1}: {exc}"
            log.error("sync run %s stopped: %s", run_id, error)
            break

        with session_factory() as session:
            run = session.get(SyncRun, run_id)
            for raw in page.records:
                _apply(session, run, raw)
            pages += 1
            run.pages = pages
            session.commit()

        log.info("sync run %s: page %s done, %s records", run_id, pages, len(page.records))

        token = page.resumption_token
        if not token:
            break
        if token in seen_tokens:  # a buggy source can hand back the same token forever
            error = f"Source repeated resumptionToken {token!r}; stopped to avoid a loop"
            log.error("sync run %s: %s", run_id, error)
            break
        seen_tokens.add(token)

    with session_factory() as session:
        run = session.get(SyncRun, run_id)
        run.finished_at = utcnow()
        run.error = error
        if error is None:
            run.status = "success"
        elif pages == 0:
            run.status = "failed"
        else:
            run.status = "partial"
        session.commit()
        log.info(
            "sync run %s %s: inserted=%s updated=%s unchanged=%s deleted=%s failed=%s",
            run.id, run.status, run.inserted, run.updated, run.unchanged, run.deleted, run.failed,
        )
        return run


def _apply(session: Session, run: SyncRun, raw) -> None:
    existing = None
    if raw.identifier:
        existing = session.scalar(select(Record).where(Record.oai_identifier == raw.identifier))

    if raw.deleted:
        if existing is not None and existing.status != "deleted":
            existing.status = "deleted"
            existing.source_datestamp = raw.datestamp
            existing.updated_at = utcnow()
            run.deleted += 1
        else:
            run.unchanged += 1
        return

    try:
        fields = normalize(raw)
    except InvalidRecord as exc:
        run.failed += 1
        session.add(
            SyncError(run_id=run.id, oai_identifier=raw.identifier, reason=str(exc), raw_xml=raw.raw_xml[:10000])
        )
        log.warning("sync run %s: rejected %s: %s", run.id, raw.identifier, exc)
        return

    columns = to_db_columns(fields)
    if existing is None:
        session.add(Record(**columns, status="active"))
        session.flush()  # so a duplicate later on the same page is found, not inserted twice
        run.inserted += 1
    elif existing.content_hash == fields["content_hash"] and existing.status == "active":
        run.unchanged += 1
    else:
        for key, value in columns.items():
            setattr(existing, key, value)
        existing.status = "active"
        existing.updated_at = utcnow()
        run.updated += 1
