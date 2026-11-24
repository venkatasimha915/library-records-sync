"""REST API and a small search page on top of the synced records."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import AfterValidator, BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import SAMPLE_DATA_DIR, Settings, get_settings
from app.db import init_db, make_engine, make_session_factory
from app.models import Record, SyncError, SyncRun
from app.oai import FileOAISource, HttpOAISource, OAISource
from app.sync import SyncAlreadyRunning, run_sync

STATIC_DIR = Path(__file__).parent / "static"


def _as_utc(value: datetime | None) -> datetime | None:
    # SQLite hands datetimes back without a timezone; we always store UTC, so say so in the JSON.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class RecordOut(BaseModel):
    id: int
    oai_identifier: str
    title: str
    creators: list[str]
    subjects: list[str]
    date: str | None
    resource_type: str | None
    language: str | None
    description: str | None
    link: str | None
    status: str
    updated_at: UtcDatetime


class RecordPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[RecordOut]


class SyncRunOut(BaseModel):
    id: int
    trigger: str
    status: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    pages: int
    inserted: int
    updated: int
    unchanged: int
    deleted: int
    failed: int
    error: str | None


class SyncErrorOut(BaseModel):
    id: int
    run_id: int
    oai_identifier: str | None
    reason: str
    created_at: UtcDatetime


def _record_out(r: Record) -> RecordOut:
    return RecordOut(
        id=r.id,
        oai_identifier=r.oai_identifier,
        title=r.title,
        creators=json.loads(r.creators),
        subjects=json.loads(r.subjects),
        date=r.date,
        resource_type=r.resource_type,
        language=r.language,
        description=r.description,
        link=r.link,
        status=r.status,
        updated_at=r.updated_at,
    )


def source_from_settings(settings: Settings) -> OAISource:
    if settings.oai_base_url == "mock":
        return FileOAISource(SAMPLE_DATA_DIR)
    return HttpOAISource(
        settings.oai_base_url,
        metadata_prefix=settings.oai_metadata_prefix,
        set_spec=settings.oai_set,
        max_retries=settings.max_retries,
        backoff_seconds=settings.retry_backoff_seconds,
        timeout_seconds=settings.request_timeout_seconds,
    )


def create_app(
    session_factory: sessionmaker | None = None,
    source_factory: Callable[[], OAISource] | None = None,
) -> FastAPI:
    settings = get_settings()
    if session_factory is None:
        engine = make_engine(settings.database_url)
        init_db(engine)
        session_factory = make_session_factory(engine)
    if source_factory is None:
        source_factory = lambda: source_from_settings(settings)  # noqa: E731

    app = FastAPI(title="Library Records Sync", version="1.0.0")

    def get_session():
        with session_factory() as session:
            yield session

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/api/records", response_model=RecordPage)
    def list_records(
        q: str | None = Query(None, max_length=200, description="Search title, creator and subject"),
        resource_type: str | None = Query(None, alias="type", max_length=128),
        status: str = Query("active", pattern="^(active|deleted|all)$"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        session: Session = Depends(get_session),
    ):
        stmt = select(Record)
        if status != "all":
            stmt = stmt.where(Record.status == status)
        if resource_type:
            stmt = stmt.where(Record.resource_type == resource_type)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(Record.title.ilike(like), Record.creators.ilike(like), Record.subjects.ilike(like))
            )
        total = session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = session.scalars(
            stmt.order_by(Record.title, Record.id).offset((page - 1) * page_size).limit(page_size)
        ).all()
        return RecordPage(total=total, page=page, page_size=page_size, items=[_record_out(r) for r in rows])

    @app.get("/api/records/{record_id}", response_model=RecordOut)
    def get_record(record_id: int, session: Session = Depends(get_session)):
        record = session.get(Record, record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Record not found")
        return _record_out(record)

    @app.post("/api/sync", response_model=SyncRunOut)
    def start_sync():
        try:
            run = run_sync(session_factory, source_factory(), trigger="api")
        except SyncAlreadyRunning:
            raise HTTPException(status_code=409, detail="A sync is already running") from None
        return SyncRunOut.model_validate(run, from_attributes=True)

    @app.get("/api/sync-runs", response_model=list[SyncRunOut])
    def list_runs(take: int = Query(20, ge=1, le=100), session: Session = Depends(get_session)):
        runs = session.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(take)).all()
        return [SyncRunOut.model_validate(r, from_attributes=True) for r in runs]

    @app.get("/api/sync-runs/{run_id}/errors", response_model=list[SyncErrorOut])
    def list_run_errors(run_id: int, session: Session = Depends(get_session)):
        if session.get(SyncRun, run_id) is None:
            raise HTTPException(status_code=404, detail="Sync run not found")
        errors = session.scalars(select(SyncError).where(SyncError.run_id == run_id).order_by(SyncError.id)).all()
        return [SyncErrorOut.model_validate(e, from_attributes=True) for e in errors]

    return app
