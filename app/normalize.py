"""Turn a Dublin Core XML record into clean fields we store and return as JSON."""

import hashlib
import json
import re
from xml.etree.ElementTree import Element

from app.oai import NS, RawRecord


class InvalidRecord(ValueError):
    pass


def _values(metadata: Element, tag: str) -> list[str]:
    seen: list[str] = []
    for el in metadata.findall(f"dc:{tag}", NS):
        value = re.sub(r"\s+", " ", (el.text or "")).strip()
        if value and value not in seen:  # drop blanks and duplicates, keep order
            seen.append(value)
    return seen


def _first(metadata: Element, tag: str) -> str | None:
    values = _values(metadata, tag)
    return values[0] if values else None


def _first_link(identifiers: list[str]) -> str | None:
    for value in identifiers:
        if value.startswith(("http://", "https://")):
            return value
    return None


def normalize(raw: RawRecord) -> dict:
    if not raw.identifier:
        raise InvalidRecord("Record header has no identifier")
    if raw.metadata is None:
        raise InvalidRecord("Record has no oai_dc metadata")

    title = _first(raw.metadata, "title")
    if not title:
        raise InvalidRecord("Record has no title")

    fields = {
        "oai_identifier": raw.identifier,
        "title": title[:2000],
        "creators": _values(raw.metadata, "creator"),
        "subjects": _values(raw.metadata, "subject"),
        "date": (_first(raw.metadata, "date") or None),
        "resource_type": _first(raw.metadata, "type"),
        "language": _first(raw.metadata, "language"),
        "description": _first(raw.metadata, "description"),
        "link": _first_link(_values(raw.metadata, "identifier")),
        "source_datestamp": raw.datestamp,
    }
    if fields["date"]:
        fields["date"] = fields["date"][:64]
    if fields["language"]:
        fields["language"] = fields["language"][:32]
    if fields["resource_type"]:
        fields["resource_type"] = fields["resource_type"][:128]
    fields["content_hash"] = content_hash(fields)
    return fields


def content_hash(fields: dict) -> str:
    """Fingerprint of the fields that matter, so we can tell 'changed' from 'same' without comparing column by column.

    The source datestamp is left out on purpose: some sources bump it without changing the record.
    """
    relevant = {k: v for k, v in fields.items() if k not in {"source_datestamp", "content_hash"}}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def to_db_columns(fields: dict) -> dict:
    out = dict(fields)
    out["creators"] = json.dumps(fields["creators"], ensure_ascii=False)
    out["subjects"] = json.dumps(fields["subjects"], ensure_ascii=False)
    return out
