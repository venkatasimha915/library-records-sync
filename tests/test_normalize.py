import json

import pytest

from app.normalize import InvalidRecord, normalize, to_db_columns
from app.oai import parse_page
from tests.conftest import dc_record, oai_page


def _raw(xml_record: str):
    return parse_page(oai_page(xml_record)).records[0]


def test_normalize_cleans_whitespace_and_duplicates():
    raw = _raw(dc_record(
        "id:1",
        title="  Soil   moisture\n data  ",
        creator=["Alvarez, Maria", "Alvarez, Maria", " Chen, David "],
        subject=["Agriculture", ""],
        identifier=["local-77", "https://repo.example.edu/items/1"],
        type="Dataset",
    ))
    fields = normalize(raw)
    assert fields["title"] == "Soil moisture data"
    assert fields["creators"] == ["Alvarez, Maria", "Chen, David"]
    assert fields["subjects"] == ["Agriculture"]
    assert fields["link"] == "https://repo.example.edu/items/1"
    assert fields["resource_type"] == "Dataset"


def test_missing_title_is_rejected():
    with pytest.raises(InvalidRecord, match="no title"):
        normalize(_raw(dc_record("id:1", title=None, creator="Someone")))


def test_missing_identifier_is_rejected():
    raw = _raw(dc_record("id:1"))
    raw.identifier = None
    with pytest.raises(InvalidRecord, match="no identifier"):
        normalize(raw)


def test_hash_ignores_datestamp_but_catches_real_changes():
    a = normalize(_raw(dc_record("id:1", title="Same")))
    b_raw = _raw(dc_record("id:1", title="Same"))
    b_raw.datestamp = "2030-01-01"
    c = normalize(_raw(dc_record("id:1", title="Different")))
    assert a["content_hash"] == normalize(b_raw)["content_hash"]
    assert a["content_hash"] != c["content_hash"]


def test_db_columns_store_lists_as_json():
    cols = to_db_columns(normalize(_raw(dc_record("id:1", creator=["Ramírez, Lucía"]))))
    assert json.loads(cols["creators"]) == ["Ramírez, Lucía"]
