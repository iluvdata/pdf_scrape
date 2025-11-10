"""PDFScrape API."""

from datetime import datetime
from enum import StrEnum
from hashlib import md5
from io import BytesIO

from aiohttp import (
    ClientConnectorError,
    ClientResponse,
    ClientResponseError,
    ClientSession,
)
from pypdf import DocumentInformation, PdfReader
from pypdf.errors import PyPdfError


class ModifiedDateSource(StrEnum):
    """Enum for how the date gets updated."""

    PDF_METADATA = "pdf_metadata"
    HTTP_HEADER = "http_header"
    FIRST_CHECK = "first_check"
    CHECKSUM = "checksum"


class PDFScrape:
    """PDFScrape Class."""

    def __init__(self) -> None:
        """Don't call as has classmethod."""
        self.pages: list[str] = []
        self.url: str
        self.modified: datetime | None = None
        self.modified_source: ModifiedDateSource | None = None
        self.session: ClientSession
        self.md5_checksum: str

    @classmethod
    async def pdfscrape(cls, url: str, session: ClientSession | None = None):
        """Instantiate a pdfscrape class."""
        self = cls()
        self.url = url
        if session is None:
            self.session = ClientSession(raise_for_status=True)
        else:
            self.session = session
        await self.update()
        return self

    async def update(self) -> None:
        """Reload the pdf."""
        try:
            resp: ClientResponse = await self.session.get(self.url)
            stream = BytesIO(await resp.read())
            pdfr: PdfReader = PdfReader(stream)
            self.pages = [page.extract_text() for page in pdfr.pages]
            metadata: DocumentInformation | None = pdfr.metadata
            if metadata:
                self.modified = metadata.modification_date
                self.modified_source = ModifiedDateSource.PDF_METADATA
            else:
                self.modified = datetime.strptime(
                    resp.headers["last-modified"], "%a, %d %b %Y %H:%M:%S %Z"
                )
                self.modified_source = ModifiedDateSource.HTTP_HEADER
            hash_md5 = md5()
            for chunk in iter(lambda: stream.read(4096), b""):
                hash_md5.update(chunk)
            self.md5_checksum = hash_md5.hexdigest()
            pdfr.close()
            stream.close()
        except PyPdfError as err:
            raise PDFParseError from err
        except (ClientResponseError, ClientConnectorError) as err:
            raise HTTPError(str(err)) from err

    def close(self) -> None:
        """Close to free up memory occupied by the pdf txt."""
        self.pages = []


class PDFParseError(PyPdfError):
    """Unable to parse pdf."""


class HTTPError(Exception):
    """issue downloading and streaming pdf."""

    def __init__(self, msg: str) -> None:
        """Initialize an HTTP Error."""
        self.msg = msg
