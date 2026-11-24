import threading

import pytest
from sqlalchemy import select

from app.config import SAMPLE_DATA_DIR
from app.models import Record, SyncError
from app.oai import FileOAISource, OAIFetchError
from app.sync import SyncAlreadyRunning, run_sync
from tests.conftest import DictSource, dc_record, oai_page


def records(session_factory) -> dict[str, Record]:
    with session_factory() as s:
        return {r.oai_identifier: r for r in s.scalars(select(Record))}


def test_sample_harvest_end_to_end(session_factory):
    run = run_sync(session_factory, FileOAISource(SAMPLE_DATA_DIR))
    assert run.status == "success"
    assert (run.pages, run.inserted, run.failed) == (2, 5, 1)
    # the deleted header refers to a record we never had, so there is nothing to delete
    assert (run.updated, run.deleted, run.unchanged) == (0, 0, 1)
    with session_factory() as s:
        errors = s.scalars(select(SyncError)).all()
        assert [e.oai_identifier for e in errors] == ["oai:repository.example.edu:1004"]
        assert "no title" in errors[0].reason


def test_second_run_with_same_data_writes_nothing(session_factory):
    run_sync(session_factory, FileOAISource(SAMPLE_DATA_DIR))
    before = {k: (r.updated_at, r.content_hash) for k, r in records(session_factory).items()}

    run = run_sync(session_factory, FileOAISource(SAMPLE_DATA_DIR))

    assert (run.inserted, run.updated, run.deleted) == (0, 0, 0)
    assert run.unchanged == 6
    after = {k: (r.updated_at, r.content_hash) for k, r in records(session_factory).items()}
    assert before == after


def test_changed_record_is_updated(session_factory):
    run_sync(session_factory, DictSource({None: oai_page(dc_record("id:1", title="Old title"))}))
    run = run_sync(session_factory, DictSource({None: oai_page(dc_record("id:1", title="New title"))}))
    assert run.updated == 1
    assert records(session_factory)["id:1"].title == "New title"


def test_deleted_record_is_marked_then_reactivated(session_factory):
    run_sync(session_factory, DictSource({None: oai_page(dc_record("id:1"))}))

    run = run_sync(session_factory, DictSource({None: oai_page(dc_record("id:1", deleted=True))}))
    assert run.deleted == 1
    assert records(session_factory)["id:1"].status == "deleted"

    again = run_sync(session_factory, DictSource({None: oai_page(dc_record("id:1", deleted=True))}))
    assert again.deleted == 0 and again.unchanged == 1  # deleting twice is not counted twice

    back = run_sync(session_factory, DictSource({None: oai_page(dc_record("id:1"))}))
    assert back.updated == 1
    assert records(session_factory)["id:1"].status == "active"


def test_duplicate_identifier_on_one_page_is_not_inserted_twice(session_factory):
    page = oai_page(dc_record("id:1", title="First"), dc_record("id:1", title="Second"))
    run = run_sync(session_factory, DictSource({None: page}))
    assert (run.inserted, run.updated) == (1, 1)
    assert records(session_factory)["id:1"].title == "Second"


def test_failure_on_later_page_keeps_earlier_pages(session_factory):
    source = DictSource({
        None: oai_page(dc_record("id:1"), token="p2"),
        "p2": OAIFetchError("Gave up after 3 attempts: HTTP 503"),
    })
    run = run_sync(session_factory, source)
    assert run.status == "partial"
    assert "Page 2" in run.error
    assert set(records(session_factory)) == {"id:1"}


def test_failure_on_first_page_marks_run_failed(session_factory):
    run = run_sync(session_factory, DictSource({None: OAIFetchError("down")}))
    assert run.status == "failed" and run.pages == 0


def test_repeated_resumption_token_stops_the_loop(session_factory):
    source = DictSource({
        None: oai_page(dc_record("id:1"), token="same"),
        "same": oai_page(dc_record("id:2"), token="same"),
    })
    run = run_sync(session_factory, source)
    assert run.status == "partial"
    assert "repeated resumptionToken" in run.error
    assert source.calls == [None, "same"]


def test_max_pages_limits_the_harvest(session_factory):
    run = run_sync(session_factory, FileOAISource(SAMPLE_DATA_DIR), max_pages=1)
    assert run.pages == 1 and run.status == "success"


def test_only_one_sync_runs_at_a_time(session_factory):
    started, release = threading.Event(), threading.Event()

    class SlowSource:
        def fetch_page(self, token):
            started.set()
            release.wait(5)
            return oai_page()

    worker = threading.Thread(target=run_sync, args=(session_factory, SlowSource()))
    worker.start()
    started.wait(5)
    try:
        with pytest.raises(SyncAlreadyRunning):
            run_sync(session_factory, DictSource({None: oai_page()}))
    finally:
        release.set()
        worker.join(5)
