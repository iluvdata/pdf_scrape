"""PDFScrape API."""

from datetime import datetime
from io import BytesIO

from aiohttp import ClientConnectorError, ClientResponseError, ClientSession
from pypdf import PdfReader
from pypdf.errors import PyPdfError


class PDFScrape:
    """PDFScrape Class."""

    def __init__(self) -> None:
        """Don't call as has classmethod."""
        self.pages: list[str] = []
        self.url: str = None
        self.modified: datetime = None

    @classmethod
    async def pdfscrape(cls, url: str):
        """Instantiate a HAPDF class."""
        self = cls()
        self.url = url
        await self.update()
        return self

    async def update(self) -> None:
        """Reload the pdf."""
        try:
            async with ClientSession(raise_for_status=True) as session:
                resp = await session.get(self.url)
                stream = BytesIO(await resp.read())
                pdfr: PdfReader = PdfReader(stream)
                self.pages = [page.extract_text() for page in pdfr.pages]
                self.modified = pdfr.metadata.modification_date
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
