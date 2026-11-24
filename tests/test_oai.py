import httpx
import pytest

from app.config import SAMPLE_DATA_DIR
from app.oai import FileOAISource, HttpOAISource, OAIFetchError, OAIProtocolError, parse_page
from tests.conftest import OAI_HEAD, dc_record, oai_page


def test_parse_page_reads_records_and_token():
    page = parse_page(oai_page(dc_record("id:1"), dc_record("id:2", deleted=True), token="abc"))
    assert [r.identifier for r in page.records] == ["id:1", "id:2"]
    assert page.records[0].deleted is False and page.records[0].metadata is not None
    assert page.records[1].deleted is True and page.records[1].metadata is None
    assert page.resumption_token == "abc"


def test_empty_resumption_token_means_last_page():
    assert parse_page(oai_page(dc_record("id:1"))).resumption_token is None


def test_no_records_match_is_an_empty_page_not_an_error():
    xml = f'{OAI_HEAD}<error code="noRecordsMatch">nothing new</error></OAI-PMH>'
    page = parse_page(xml)
    assert page.records == [] and page.resumption_token is None


def test_other_oai_errors_raise():
    xml = f'{OAI_HEAD}<error code="badResumptionToken">expired</error></OAI-PMH>'
    with pytest.raises(OAIProtocolError, match="badResumptionToken"):
        parse_page(xml)


def test_invalid_xml_raises():
    with pytest.raises(OAIProtocolError):
        parse_page("<html>Service unavailable")


def test_xml_entity_attacks_are_blocked():
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>&b;</ListRecords></OAI-PMH>'
    )
    with pytest.raises(OAIProtocolError):
        parse_page(bomb)


def test_sample_data_pages_parse():
    source = FileOAISource(SAMPLE_DATA_DIR)
    first = parse_page(source.fetch_page(None))
    second = parse_page(source.fetch_page(first.resumption_token))
    assert len(first.records) == 4 and first.resumption_token == "page2"
    assert len(second.records) == 3 and second.resumption_token is None


def test_file_source_rejects_path_tricks():
    with pytest.raises(OAIFetchError):
        FileOAISource(SAMPLE_DATA_DIR).fetch_page("../config")


def _http_source(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    source = HttpOAISource("https://oai.example.edu/oai", client=client, sleep=sleeps.append, **kwargs)
    return source, sleeps


def test_http_first_request_params_and_token_request_params():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, text=oai_page())

    source, _ = _http_source(handler, set_spec="theses")
    source.fetch_page(None)
    source.fetch_page("tok123")
    assert seen[0] == {"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": "theses"}
    assert seen[1] == {"verb": "ListRecords", "resumptionToken": "tok123"}  # no other args, per the spec


def test_http_retries_temporary_failures_with_backoff():
    responses = iter([httpx.Response(503), httpx.Response(429), httpx.Response(200, text="ok")])
    source, sleeps = _http_source(lambda request: next(responses), max_retries=3, backoff_seconds=1.0)
    assert source.fetch_page(None) == "ok"
    assert sleeps == [1.0, 2.0]


def test_http_retries_network_errors_then_gives_up():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    source, sleeps = _http_source(handler, max_retries=3, backoff_seconds=0.5)
    with pytest.raises(OAIFetchError, match="Gave up after 3 attempts"):
        source.fetch_page(None)
    assert sleeps == [0.5, 1.0]


def test_http_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    source, sleeps = _http_source(handler)
    with pytest.raises(OAIFetchError, match="HTTP 404"):
        source.fetch_page(None)
    assert len(calls) == 1 and sleeps == []
