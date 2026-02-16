"""PDFScrape API."""

from abc import ABC, abstractmethod
import datetime
from enum import StrEnum
from functools import partial
from hashlib import md5
from io import BufferedReader, BytesIO
from pathlib import Path
from typing import IO, Any, Final, cast

from aiohttp import (
    ClientConnectorError,
    ClientResponse,
    ClientResponseError,
    ClientSession,
)
from pypdf import DocumentInformation, PdfReader
from pypdf.errors import PyPdfError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORE_VERSION: Final[int] = 1


class ModifiedDateSource(StrEnum):
    """Enum for how the date gets updated."""

    PDF_METADATA = "pdf_metadata"
    HTTP_HEADER = "http_header"
    FILE_MTIME = "file_modification_time"
    FIRST_CHECK = "first_check"
    CHECKSUM = "checksum"
    UPLOAD = "upload"


type StoredFile = dict[
    str, list[str] | datetime.datetime | ModifiedDateSource | str | None
]


def get_store(hass: HomeAssistant, key: str) -> Store[StoredFile]:
    """Get a store."""
    return Store[StoredFile](hass, STORE_VERSION, f"{DOMAIN}_{key}")


class PDFScrape(ABC):
    """PDFScrape Base Class."""

    def __init__(self, hass: HomeAssistant, config_entry_id: str | None) -> None:
        """Called by classmethod with is called by the subclass."""
        self.hass: HomeAssistant = hass
        self.pages: list[str] = []
        self.modified: datetime.datetime | None = None
        self.modified_source: ModifiedDateSource | None = None
        self.metadata_name: str | None = None
        self.md5_checksum: str
        self.stored_file: StoredFile | None = None
        self.config_entry_id: str | None = config_entry_id
        # if config_entry_id is None that means this a config flow and so just sents the file
        self.store: Store[StoredFile] | None = (
            get_store(hass, self.config_entry_id)
            if self.config_entry_id is not None
            else None
        )

    async def _pdf_scrape(self):
        if self.store is not None:
            self.stored_file = await self.store.async_load()
            if not self.stored_file:
                self.stored_file = {}
        await self.update()

    async def _process_pdf(
        self,
        stream: IO[Any],
        alt_timestamp: tuple[datetime.datetime, ModifiedDateSource] | None = None,
        upload: bool = False,
    ) -> bool:
        """(Re)load a pdf from a url."""
        # return true is updated.
        try:
            pdfr: PdfReader = PdfReader(stream)
            metadata: DocumentInformation | None = pdfr.metadata
            if metadata:
                self.modified = metadata.modification_date
                self.modified_source = ModifiedDateSource.PDF_METADATA
                if upload:
                    self.metadata_name = metadata.title
                if self.modified is not None:
                    self.modified = self.modified.replace(tzinfo=datetime.UTC)
            if self.modified is None and alt_timestamp is not None:
                self.modified, self.modified_source = alt_timestamp
            hash_md5 = md5()
            for chunk in iter(lambda: stream.read(4096), b""):
                hash_md5.update(chunk)
            self.md5_checksum = hash_md5.hexdigest()
            # Check if there are changes, otherwise we should stop to save comp time
            if (
                self.stored_file
                and self.modified
                == datetime.datetime.fromisoformat(self.stored_file.get("modified"))
                and self.md5_checksum == self.stored_file.get("md5_checksum")
            ):
                pdfr.close()
                if isinstance(stream, BytesIO):
                    stream.close()
                return False
            self.pages = [page.extract_text() for page in pdfr.pages]
            pdfr.close()
            if isinstance(stream, BytesIO):
                stream.close()
        except PyPdfError as err:
            raise PDFParseError from err
        if self.store is not None:
            if self.stored_file is None:
                self.stored_file = {}
            self.stored_file["modified"] = self.modified.isoformat()
            self.stored_file["md5_checksum"] = self.md5_checksum
            self.stored_file["modified_source"] = self.modified_source
            self.stored_file["pages"] = self.pages
            await self.store.async_save(self.stored_file)
        return True

    async def _load_from_storage(self) -> None:
        if self.store is not None:
            self.stored_file = await self.store.async_load()
            if self.stored_file is not None:
                self.pages = cast(list[str], self.stored_file.get("pages"))
                self.modified = datetime.datetime.fromisoformat(
                    self.stored_file.get("modified")
                )
                self.modified_source = cast(
                    ModifiedDateSource, self.stored_file.get("modified_source")
                )
                self.md5_checksum = cast(str, self.stored_file["md5_checksum"])
                return
        raise StoredFileError

    @abstractmethod
    async def update(self) -> bool:
        """Must be implemented by sub_classes."""

    def close(self) -> None:
        """Close to free up memory occupied by the pdf txt."""
        self.pages = []

    def get_pages(self, page_range: str) -> str:
        """Parse page range string into list of page numbers."""

        page_nums: set[int] = set()
        for part in page_range.split(","):
            if "-" in part:
                start_str, end_str = part.split("-")
                start, end = int(start_str), int(end_str)
                page_nums.update(range(start, end + 1))
            else:
                page_nums.add(int(part))
        if max(page_nums) > len(self.pages) or min(page_nums) < 1:
            raise ValueError("Page number out of range")
        return "\n".join(self.pages[page - 1] for page in sorted(page_nums))


class PDFScrapeHTTP(PDFScrape):
    """Parse pdf from http/https source."""

    def __init__(self, hass: HomeAssistant, config_entry_id: str | None) -> None:
        """Call only from classmethod."""
        super().__init__(hass, config_entry_id)
        self.url: str
        self.session: ClientSession

    @classmethod
    async def pdfscrape(
        cls,
        hass: HomeAssistant,
        url: str,
        *,
        config_entry_id: str | None = None,
        session: ClientSession | None = None,
    ):
        """Instantiate a pdfscrape class."""
        self = cls(hass, config_entry_id)
        self.url = url
        if session is None:
            self.session = ClientSession(raise_for_status=True)
        else:
            self.session = session
        await self._pdf_scrape()
        return self

    def __repr__(self) -> str:
        """Representation."""
        return f"PDF({self.url})"

    async def update(self) -> bool:
        """(Re)load a pdf from a URL."""
        try:
            resp: ClientResponse = await self.session.get(self.url)
            stream: BytesIO = BytesIO(await resp.read())
            alt_modified: datetime
            alt_modified_source: ModifiedDateSource
            if resp.headers.get("late-modified"):
                alt_modified = datetime.strptime(
                    resp.headers["last-modified"], "%a, %d %b %Y %H:%M:%S %Z"
                )
                alt_modified_source = ModifiedDateSource.HTTP_HEADER
            else:
                alt_modified = datetime.datetime.now(datetime.UTC)
                alt_modified_source = ModifiedDateSource.FIRST_CHECK
            if await self._process_pdf(
                stream,
                (alt_modified, alt_modified_source),
            ):
                return True
            await self._load_from_storage()

        except (ClientResponseError, ClientConnectorError) as err:
            raise HTTPError(str(err)) from err

        return False


class PDFScrapeFile(PDFScrape):
    """Parse pdf from file."""

    def __init__(
        self, hass: HomeAssistant, config_entry_id: str | None, path: Path | str | None
    ) -> None:
        """Call only from classmethod."""
        super().__init__(hass, config_entry_id)
        self.path: Path | None = (
            path if isinstance(path, Path) or path is None else Path(path)
        )

    @classmethod
    async def pdfscrape(
        cls,
        hass: HomeAssistant,
        path: Path | str | None,
        *,
        config_entry_id: str | None = None,
    ):
        """Initialize a PDFScrapeFile class."""
        self = cls(hass, config_entry_id, path)
        await self._pdf_scrape()
        return self

    async def _update(self, upload: bool = False) -> bool:
        """Check for an update."""
        if self.path is not None:
            try:
                stream: BufferedReader = await self.hass.async_add_executor_job(
                    partial(self.path.open, mode="rb")
                )
                modified: datetime = (
                    datetime.datetime.now(datetime.UTC)
                    if upload
                    else datetime.datetime.fromtimestamp(
                        self.path.stat().st_mtime, datetime.UTC
                    )
                )
                if await self._process_pdf(
                    stream,
                    (
                        modified,
                        ModifiedDateSource.UPLOAD
                        if upload
                        else ModifiedDateSource.FILE_MTIME,
                    ),
                    upload=upload,
                ):
                    return True
            except OSError as err:
                raise FileError(str(err)) from err
        # We have an upload without an update, pull from file.
        await self._load_from_storage()
        return False


class PDFScrapeUpload(PDFScrapeFile):
    """Upload PDF Scape."""

    @classmethod
    async def pdfscrape(
        cls,
        hass: HomeAssistant,
        *,
        path: Path | None = None,
        config_entry_id: str | None = None,
    ):
        """Initialize a PDFScrapeUpload class."""
        if path is None and config_entry_id is None:
            raise ValueError("Either path or config_entry_id must be specified")
        self = cls(hass, config_entry_id, path)
        await self._pdf_scrape()
        return self

    def __repr__(self):
        """Representation."""
        return "PDF(Uploaded)"

    async def update(self) -> bool:
        """(Re)load a pdf from an upload."""
        return await self._update(upload=True)


class PDFScrapeLocal(PDFScrapeFile):
    """Upload PDF Scape."""

    def __repr__(self):
        """Representation."""
        return f"PDF({self.path})"

    async def update(self) -> bool:
        """(Re)load a pdf from an upload."""
        return await self._update()


class PDFParseError(PyPdfError):
    """Unable to parse pdf."""


class StoredFileError(Exception):
    """Error accessing the parsed pdf."""


class FileError(Exception):
    """Issue opening uploaded pdf."""


class HTTPError(Exception):
    """issue downloading and streaming pdf."""

    def __init__(self, msg: str) -> None:
        """Initialize an HTTP Error."""
        self.msg = msg
