"""PDF Scrape File Handling."""

from abc import ABC, abstractmethod
from asyncio import Task, TaskGroup
import datetime
from enum import StrEnum
from functools import partial
from hashlib import file_digest
from io import BytesIO
import logging
from pathlib import Path
import re
from typing import Any, Final, cast, override

from httpx import HTTPStatusError, RequestError, Response
from PIL import Image
from pydantic import BaseModel, Field
from pymupdf import Document, Pixmap, TextPage

from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.storage import STORAGE_DIR, Store
from homeassistant.util.dt import utcnow

from .const import DOMAIN

STORE_VERSION: Final[int] = 2

_LOGGER = logging.getLogger(__name__)


class ModifiedDateSource(StrEnum):
    """Enum for how the date gets updated."""

    PDF_METADATA = "pdf_metadata"
    HTTP_HEADER = "http_header"
    FILE_MTIME = "file_modification_time"
    FIRST_CHECK = "first_check"
    CHECKSUM = "checksum"
    UPLOAD = "upload"


class PDF(BaseModel):
    """Class for the stored file in storage."""

    modified: datetime.datetime | None = None
    title: str | None = None
    sha256_checksum: str | None = None
    modified_source: ModifiedDateSource | None = None
    page_count: int = 0
    pages: dict[int, str] = {}
    http_headers: HTTPHeaders | None = None
    loaded_from_store: bool = Field(exclude=True, default=False)


class HTTPHeaders(BaseModel):
    """Class for HTTP headers (to avoid constantly fetching the entire pdf)."""

    last_modified: datetime.datetime
    content_length: int


class _PDFStore(Store[PDF]):
    """PDF Store."""

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        """Init a store."""
        super().__init__(hass, STORE_VERSION, key)

    @override
    async def _async_migrate_func(
        self, old_major_version: Any, old_minor_version: Any, old_data: Any
    ) -> Any:
        if old_major_version != STORE_VERSION:
            data: dict[str, Any] = {**old_data}
            if "md5_checksum" in data:
                data.pop("md5_checksum")
            pages_obj = data["pages"]
            if isinstance(pages_obj, list):
                data.pop("pages")
                data["pages"] = {}
                for i, page in enumerate(cast(list[str], pages_obj)):
                    data["pages"][i + 1] = page
                data["page_count"] = len(data["pages"])
            return data
        raise StoredFileError(f"Unable to migrate to store version {STORE_VERSION}")


def get_store(hass: HomeAssistant, key: str) -> _PDFStore:
    """Get a store."""
    return _PDFStore(hass, f"{DOMAIN}_{key}")


class PDFScrape(ABC):
    """PDFScrape Base Class."""

    def __init__(self, hass: HomeAssistant, config_entry_id: str | None) -> None:
        """Called by classmethod with is called by the subclass."""
        self.hass: HomeAssistant = hass
        self._document: Document
        self.pdf: PDF = PDF()
        self.config_entry_id: str | None = config_entry_id
        self._stream: BytesIO

        # if config_entry_id is None that means this a config flow and so just sends the file
        self.store: _PDFStore | None = (
            get_store(hass, self.config_entry_id)
            if self.config_entry_id is not None
            else None
        )

    async def _pdf_scrape(self):
        if self.store is not None:
            if stored_file := await self.store.async_load():
                self.pdf = PDF(**stored_file, loaded_from_store=True)
        await self.update()

    async def _process_pdf(
        self,
        alt_timestamp: tuple[datetime.datetime, ModifiedDateSource] | None = None,
    ) -> bool:
        """(Re)load a pdf from a url.

        returns true if the pdf was updated (either modified date or checksum), false if not.
        """
        _LOGGER.debug("Start processing PDF")
        self._document = await self.hass.async_add_executor_job(
            partial(Document, stream=self._stream)
        )
        modified: datetime.datetime | None = None
        title: str | None = None
        if self._document.metadata:
            if "title" in self._document.metadata:
                title = self._document.metadata["title"]
            if "modDate" in self._document.metadata:
                matches: re.Match[str] | None = re.search(
                    r"(\d{14}-\d{2})(?:')(\d{2})(?:')",
                    self._document.metadata["modDate"],
                )
                if matches:
                    modified = datetime.datetime.strptime(
                        f"{matches.group(1)}{matches.group(2)}", "%Y%m%d%H%M%S%z"
                    )
                    self.pdf.modified_source = ModifiedDateSource.PDF_METADATA
                    modified.replace(tzinfo=datetime.UTC)
        if modified is None and alt_timestamp is not None:
            modified, self.pdf.modified_source = alt_timestamp
        self._stream.seek(0)  # reset pointer
        digest_sha256 = await self.hass.async_add_executor_job(
            file_digest, self._stream, "sha256"
        )
        sha256_checksum: str = digest_sha256.hexdigest()
        # Check if there are changes, otherwise we should stop to save comp time
        if (
            self.pdf.loaded_from_store
            and modified == self.pdf.modified
            and sha256_checksum == self.pdf.sha256_checksum
        ):
            await self.close()
            _LOGGER.debug("PDF not modified since last load, skipping processing")
            return False
        self.pdf.modified = modified
        self.pdf.sha256_checksum = sha256_checksum
        self.pdf.title = title
        self.pdf.page_count = self._document.page_count
        if self.store is not None:
            if len(self.pdf.pages) > 0:
                # already loaded pages, do we need to recheck?
                await self._get_pages(set(self.pdf.pages.keys()), update=True)
            await self.save_to_store()
            # Generate a thumbnail
            pixmap: Pixmap = await self.hass.async_add_executor_job(
                self._document[0].get_pixmap
            )
            # Resize to 512x512 maintining aspect ratio
            pil_image: Image.Image = pixmap.pil_image()
            await self.hass.async_add_executor_job(
                pil_image.thumbnail, (512, 512), Image.Resampling.BICUBIC
            )
            pdf_storage_path: Path = Path(
                self.hass.config.path(
                    STORAGE_DIR,
                    DOMAIN,
                )
            )
            if not pdf_storage_path.exists():
                await self.hass.async_add_executor_job(pdf_storage_path.mkdir)
            await self.hass.async_add_executor_job(
                pil_image.save,
                f"{pdf_storage_path}/{self.config_entry_id}.webp",
                "WEBP",
            )
            # save the actual pdf file (if neeeded)
            if isinstance(self, (PDFScrapeHTTP, PDFScrapeUpload)):
                self._stream.seek(0)
                path: Path = Path(f"{pdf_storage_path}/{self.config_entry_id}.pdf")
                with await self.hass.async_add_executor_job(
                    partial(
                        path.open,
                        mode="wb",
                    )
                ) as f:
                    memview = self._stream.getbuffer()
                    await self.hass.async_add_executor_job(f.write, memview)
                    memview.release()
        await self.close()
        _LOGGER.debug("PDF Finished Processing")
        return True

    @abstractmethod
    async def update(self) -> bool:
        """Must be implemented by sub_classes."""

    async def close(self) -> None:
        """Close to free up memory occupied by the pdf and file lock."""
        if hasattr(self, "_document") and not self._document.is_closed:
            await self.hass.async_add_executor_job(self._document.close)
        if hasattr(self, "_stream") and not self._stream.closed:
            self._stream.close()

    async def save_to_store(self) -> None:
        """Save the PDF to the store."""
        if self.store is not None:
            await self.store.async_save(self.pdf.model_dump())

    async def _get_pages(
        self,
        page_nums: set[int],
        update: bool = False,
    ) -> int:
        """Get txt on a pages."""
        tasks: dict[int, Task] = {}
        async with TaskGroup() as tg:
            for page in page_nums:
                page_index = page - 1
                if page_index not in self.pdf.pages or update:
                    tasks[page_index] = tg.create_task(self._get_page_text(page_index))
        for page_index, task in tasks.items():
            if task.exception():
                _LOGGER.exception(
                    "Error processing page %s",
                    page_index + 1,
                    exc_info=task.exception(),
                )
            self.pdf.pages[page_index] = task.result()

    async def _load_stream_from_file(self, file: Path) -> None:
        """Load the file into a stream."""
        with await self.hass.async_add_executor_job(partial(file.open, mode="rb")) as f:
            self._stream = BytesIO(await self.hass.async_add_executor_job(f.read))

    async def _load_document_from_file_or_cache(self) -> None:
        """Load the document from file or cache."""
        if (
            hasattr(self, "_document")
            and self._document is not None
            and not self._document.is_closed
        ):
            return
        if hasattr(self, "file"):
            await self._load_stream_from_file(self.file)
        else:
            await self._load_stream_from_file(
                Path(
                    self.hass.config.path(
                        STORAGE_DIR, DOMAIN, f"{self.config_entry_id}.pdf"
                    )
                )
            )
        self._document = await self.hass.async_add_executor_job(
            partial(Document, stream=self._stream)
        )

    async def _get_page_text(self, page_index: int) -> str:
        """Get text from a specific page."""
        await self._load_document_from_file_or_cache()

        def wrap_extract_text() -> str:
            text_page: TextPage = self._document[page_index].get_textpage()
            return text_page.extractText()

        return await self.hass.async_add_executor_job(wrap_extract_text)

    async def get_pages(self, page_range: str, ocr: bool = False) -> str:
        """Parse page range string into list of page numbers."""
        page_nums: set[int] = set()
        for part in page_range.split(","):
            if "-" in part:
                start_str, end_str = part.split("-")
                start, end = int(start_str), int(end_str)
                page_nums.update(range(start, end + 1))
            else:
                page_nums.add(int(part))
        if max(page_nums) > self.pdf.page_count or min(page_nums) < 1:
            raise IndexError("Page number out of range")
        await self._get_pages(page_nums, ocr)
        return "\n".join(self.pdf.pages[page - 1] or "" for page in sorted(page_nums))


class PDFScrapeHTTP(PDFScrape):
    """Parse pdf from http/https source."""

    def __init__(
        self, hass: HomeAssistant, url: str, config_entry_id: str | None = None
    ) -> None:
        """Call from class method unless need to monitor progress on first load."""
        super().__init__(hass, config_entry_id)
        self.url: str = url

    @classmethod
    async def pdfscrape(
        cls,
        hass: HomeAssistant,
        url: str,
        *,
        config_entry_id: str | None = None,
    ):
        """Instantiate a pdfscrape class."""
        self = cls(hass, url, config_entry_id)
        await self._pdf_scrape()
        return self

    def __repr__(self) -> str:
        """Representation."""
        return f"PDF({self.url})"

    async def update(self) -> bool:
        """(Re)load a pdf from a URL."""
        try:
            if self.pdf.http_headers is not None:
                async with get_async_client(self.hass) as client:
                    r: Response = await client.head(self.url)
                    if "last-modified" in r.headers and "content-length" in r.headers:
                        new_headers = HTTPHeaders(
                            last_modified=convert_header_date(
                                r.headers["last-modified"]
                            ),
                            content_length=int(r.headers["content-length"]),
                        )

                        if new_headers == self.pdf.http_headers:
                            _LOGGER.debug(
                                "HTTP headers indicate PDF has not changed, skipping download"
                            )
                            return False
            async with (
                get_async_client(self.hass) as client,
                client.stream("GET", self.url) as r,
            ):
                r.raise_for_status()
                self._stream = BytesIO()
                async for chunk in r.aiter_bytes():
                    self._stream.write(chunk)
                self.pdf.http_headers = HTTPHeaders(
                    last_modified=convert_header_date(r.headers["last-modified"]),
                    content_length=int(r.headers.get("content-length")),
                )
                alt_modified: datetime
                alt_modified_source: ModifiedDateSource
                if self.pdf.http_headers.last_modified is not None:
                    alt_modified = self.pdf.http_headers.last_modified
                    alt_modified_source = ModifiedDateSource.HTTP_HEADER
                else:
                    alt_modified = utcnow()
                    alt_modified_source = ModifiedDateSource.FIRST_CHECK
            return await self._process_pdf((alt_modified, alt_modified_source))
        except (RequestError, HTTPStatusError) as err:
            raise HTTPError(str(err)) from err


def convert_header_date(date_str: str) -> datetime.datetime:
    """Convert HTTP header date to datetime."""
    return datetime.datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %Z").replace(
        tzinfo=datetime.UTC
    )


class PDFScrapeFile(PDFScrape):
    """Parse pdf from file."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry_id: str | None,
        file: Path | str,
    ) -> None:
        """Call only from classmethod."""
        super().__init__(hass, config_entry_id)
        self.file: Path = file if isinstance(file, Path) else Path(file)

    @classmethod
    async def pdfscrape(
        cls,
        hass: HomeAssistant,
        file: Path | str,
        *,
        config_entry_id: str | None = None,
    ):
        """Initialize a PDFScrapeFile class."""
        self = cls(hass, config_entry_id, file)
        await self._pdf_scrape()
        return self

    async def update(self) -> bool:
        """Check for an update."""
        if self.file is not None:
            try:
                modified: datetime = datetime.datetime.fromtimestamp(
                    (await self.hass.async_add_job_executor(self.file.stat)).st_mtime,
                    datetime.UTC,
                )
                with await self.hass.async_add_executor_job(
                    partial(self.file.open, mode="rb")
                ) as f:
                    self._stream = BytesIO(
                        await self.hass.async_add_executor_job(f.read)
                    )
                if await self._process_pdf(
                    (
                        modified,
                        ModifiedDateSource.FILE_MTIME,
                    )
                ):
                    return True
            except OSError as err:
                raise FileError(str(err)) from err
        return False

    def __repr__(self):
        """Representation."""
        return f"PDF({self.file})" if self.file is not None else "PDF(Local File)"


class PDFScrapeUpload(PDFScrape):
    """Upload PDF Scape."""

    def __init__(
        self, hass: HomeAssistant, config_entry_id: str, file: Path | str | None = None
    ) -> None:
        """Initialize for cached files only."""
        super().__init__(hass, config_entry_id)
        if file:
            if isinstance(file, str):
                file = Path(file)
            with file.open(mode="rb") as pdf_file:
                self._stream = BytesIO()
                self._stream.write(pdf_file.read())

    @classmethod
    async def async_from_file(
        cls,
        hass: HomeAssistant,
        file: Path | str,
        config_entry_id: str,
    ):
        """Initialize a PDFScrapeUpload class but do not process."""
        self = cls(hass, config_entry_id)
        if isinstance(file, str):
            file = Path(file)
        with await self.hass.async_add_executor_job(
            partial(file.open, mode="rb")
        ) as pdf_file:
            self._stream = BytesIO()
            self._stream.write(await self.hass.async_add_executor_job(pdf_file.read))
        return self

    @classmethod
    async def pdfscrape(
        cls, hass: HomeAssistant, config_entry_id: str, file: Path | str | None = None
    ):
        """Initialize a PDFScrapeUpload class."""
        if file is None:
            self = cls(hass, config_entry_id)
        else:
            self = await cls.from_file(hass, file, config_entry_id)
        await self._pdf_scrape()
        return self

    async def update(self) -> bool:
        """Check for an update."""
        if hasattr(self, "_stream"):
            return await self._process_pdf((utcnow(), ModifiedDateSource.UPLOAD))
        return False

    def __repr__(self):
        """Representation."""
        return f"PDF Uploaded - {self.pdf.title}" if self.pdf.title else "PDF Uploaded"


class StoredFileError(Exception):
    """Error accessing the parsed pdf."""


class FileError(Exception):
    """Issue opening uploaded pdf."""


class HTTPError(Exception):
    """issue downloading and streaming pdf."""

    def __init__(self, msg: str) -> None:
        """Initialize an HTTP Error."""
        self.msg = msg
