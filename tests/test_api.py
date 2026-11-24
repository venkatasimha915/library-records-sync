import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import SAMPLE_DATA_DIR
from app.oai import FileOAISource
from app.sync import _run_lock


@pytest.fixture
def client(session_factory):
    app = create_app(session_factory=session_factory, source_factory=lambda: FileOAISource(SAMPLE_DATA_DIR))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def synced(client):
    assert client.post("/api/sync").status_code == 200
    return client


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_sync_endpoint_returns_run_summary(client):
    body = client.post("/api/sync").json()
    assert body["status"] == "success"
    assert body["inserted"] == 5 and body["failed"] == 1


def test_sync_endpoint_returns_409_when_busy(client):
    _run_lock.acquire()
    try:
        assert client.post("/api/sync").status_code == 409
    finally:
        _run_lock.release()


def test_list_records_sorted_and_paged(synced):
    body = synced.get("/api/records", params={"page_size": 2}).json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    titles = [i["title"] for i in body["items"]]
    assert titles == sorted(titles)
    page3 = synced.get("/api/records", params={"page_size": 2, "page": 3}).json()
    assert len(page3["items"]) == 1


def test_search_matches_title_creator_and_subject(synced):
    def hits(q):
        return {i["oai_identifier"] for i in synced.get("/api/records", params={"q": q}).json()["items"]}

    assert hits("railroad") == {"oai:repository.example.edu:1002"}
    assert hits("patel") == {"oai:repository.example.edu:1003"}
    assert hits("architecture") == {"oai:repository.example.edu:1005"}
    assert hits("no such thing") == set()


def test_filter_by_type(synced):
    items = synced.get("/api/records", params={"type": "Dataset"}).json()["items"]
    assert [i["resource_type"] for i in items] == ["Dataset"]


def test_record_detail_and_404(synced):
    first = synced.get("/api/records").json()["items"][0]
    detail = synced.get(f"/api/records/{first['id']}").json()
    assert detail["oai_identifier"] == first["oai_identifier"]
    assert isinstance(detail["creators"], list)
    assert synced.get("/api/records/999999").status_code == 404


def test_bad_query_params_are_rejected(client):
    assert client.get("/api/records", params={"page_size": 1000}).status_code == 422
    assert client.get("/api/records", params={"status": "bogus"}).status_code == 422


def test_sync_runs_and_errors(synced):
    runs = synced.get("/api/sync-runs").json()
    assert len(runs) == 1
    assert runs[0]["started_at"].endswith(("Z", "+00:00"))  # times are sent as UTC
    errors = synced.get(f"/api/sync-runs/{runs[0]['id']}/errors").json()
    assert errors[0]["oai_identifier"] == "oai:repository.example.edu:1004"
    assert synced.get("/api/sync-runs/999/errors").status_code == 404


def test_search_page_has_basic_accessibility(client):
    html = client.get("/").text
    assert '<html lang="en">' in html
    assert '<label for="q">' in html and 'id="q"' in html
    assert '<label for="type">' in html and 'id="type"' in html
    assert 'aria-live="polite"' in html
    assert 'href="#main"' in html  # skip link
    assert "<caption>" in html
