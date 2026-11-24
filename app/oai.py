"""Reading records from an OAI-PMH source.

OAI-PMH is the XML protocol most library repositories and catalogs use to share metadata.
A harvest calls ListRecords, gets one page of XML, and keeps calling with the
resumptionToken until the source stops sending one.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from xml.etree.ElementTree import Element, tostring

import httpx
from defusedxml import ElementTree as SafeET  # blocks XML bombs / external entity attacks

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


class OAIFetchError(Exception):
    """The source could not be reached or kept failing after retries."""


class OAIProtocolError(Exception):
    """The source answered, but with an OAI error or XML we can't read."""


@dataclass
class RawRecord:
    identifier: str | None
    datestamp: str | None
    deleted: bool
    metadata: Element | None
    raw_xml: str


@dataclass
class Page:
    records: list[RawRecord]
    resumption_token: str | None


class OAISource(Protocol):
    def fetch_page(self, resumption_token: str | None) -> str: ...


def parse_page(xml_text: str) -> Page:
    try:
        root = SafeET.fromstring(xml_text)
    except Exception as exc:  # defusedxml raises several exception types
        raise OAIProtocolError(f"Response is not valid XML: {exc}") from exc

    error = root.find("oai:error", NS)
    if error is not None:
        code = error.get("code", "unknown")
        if code == "noRecordsMatch":  # an empty result is normal, not a failure
            return Page(records=[], resumption_token=None)
        raise OAIProtocolError(f"OAI error {code}: {(error.text or '').strip()}")

    list_records = root.find("oai:ListRecords", NS)
    if list_records is None:
        raise OAIProtocolError("Response has no ListRecords element")

    records = []
    for rec in list_records.findall("oai:record", NS):
        header = rec.find("oai:header", NS)
        identifier = _text(header, "oai:identifier") if header is not None else None
        datestamp = _text(header, "oai:datestamp") if header is not None else None
        deleted = header is not None and header.get("status") == "deleted"
        metadata = rec.find("oai:metadata/oai_dc:dc", NS)
        records.append(
            RawRecord(
                identifier=identifier,
                datestamp=datestamp,
                deleted=deleted,
                metadata=metadata,
                raw_xml=tostring(rec, encoding="unicode"),
            )
        )

    token_el = list_records.find("oai:resumptionToken", NS)
    token = (token_el.text or "").strip() if token_el is not None else ""
    return Page(records=records, resumption_token=token or None)


def _text(parent: Element, path: str) -> str | None:
    el = parent.find(path, NS)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


class HttpOAISource:
    """Talks to a real OAI-PMH endpoint, with retries for the failures that are usually temporary."""

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        base_url: str,
        metadata_prefix: str = "oai_dc",
        set_spec: str | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url
        self.metadata_prefix = metadata_prefix
        self.set_spec = set_spec
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self.sleep = sleep

    def build_params(self, resumption_token: str | None) -> dict[str, str]:
        # The OAI-PMH spec says a resumptionToken request must not repeat the other arguments.
        if resumption_token:
            return {"verb": "ListRecords", "resumptionToken": resumption_token}
        params = {"verb": "ListRecords", "metadataPrefix": self.metadata_prefix}
        if self.set_spec:
            params["set"] = self.set_spec
        return params

    def fetch_page(self, resumption_token: str | None) -> str:
        params = self.build_params(resumption_token)
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.get(self.base_url, params=params)
            except httpx.TransportError as exc:
                last_error = f"network error: {exc}"
            else:
                if response.status_code == 200:
                    return response.text
                if response.status_code not in self.RETRY_STATUS:
                    raise OAIFetchError(f"HTTP {response.status_code} from source (not retried)")
                last_error = f"HTTP {response.status_code}"

            if attempt < self.max_retries:
                self.sleep(self.backoff_seconds * 2 ** (attempt - 1))  # 1s, 2s, 4s ...

        raise OAIFetchError(f"Gave up after {self.max_retries} attempts: {last_error}")


class FileOAISource:
    """Reads saved OAI-PMH pages from disk. Used for the demo and tests, so nothing depends on a live server.

    The first page is page1.xml; a resumptionToken of "page2" loads page2.xml, and so on.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def fetch_page(self, resumption_token: str | None) -> str:
        name = resumption_token or "page1"
        path = self.directory / f"{name}.xml"
        if not path.is_file() or path.parent.resolve() != self.directory.resolve():
            raise OAIFetchError(f"No sample page named {name!r}")
        return path.read_text(encoding="utf-8")
