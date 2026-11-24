"""Shared test setup.

By default tests use an in-memory SQLite database. Set TEST_DATABASE_URL to run the same
tests against PostgreSQL (CI does this), e.g.
    TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/records_test pytest
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.db import make_session_factory
from app.models import Base

OAI_HEAD = '<?xml version="1.0" encoding="UTF-8"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
DC_OPEN = (
    '<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
)


def dc_record(oai_id: str, title: str | None = "A title", deleted: bool = False, **fields) -> str:
    status = ' status="deleted"' if deleted else ""
    header = f"<header{status}><identifier>{oai_id}</identifier><datestamp>2026-11-20</datestamp></header>"
    if deleted:
        return f"<record>{header}</record>"
    parts = []
    if title is not None:
        parts.append(f"<dc:title>{title}</dc:title>")
    for tag, value in fields.items():
        for v in value if isinstance(value, list) else [value]:
            parts.append(f"<dc:{tag}>{v}</dc:{tag}>")
    return f"<record>{header}<metadata>{DC_OPEN}{''.join(parts)}</oai_dc:dc></metadata></record>"


def oai_page(*records: str, token: str | None = None) -> str:
    token_xml = f"<resumptionToken>{token}</resumptionToken>" if token else "<resumptionToken/>"
    return f"{OAI_HEAD}<ListRecords>{''.join(records)}{token_xml}</ListRecords></OAI-PMH>"


class DictSource:
    """Fake OAI source: maps resumptionToken (None for page one) to an XML string or an exception."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[str | None] = []

    def fetch_page(self, resumption_token):
        self.calls.append(resumption_token)
        value = self.pages[resumption_token]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        eng = create_engine(url)
    else:
        eng = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)
