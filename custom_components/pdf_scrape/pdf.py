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
from typing import Final

from httpx import HTTPStatusError, RequestError, Response
from PIL import Image
from pydantic import BaseModel, Field
from pymupdf import Document, Pixmap, TextPage
from pymupdf4llm import to_text

from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.storage import STORAGE_DIR, Store

from .const import DOMAIN

STORE_VERSION: Final[int] = 1

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
    pages: dict[int, PDFPage] = {}
    http_headers: HTTPHeaders | None = None
    loaded_from_store: bool = Field(exclude=True, default=False)


class HTTPHeaders(BaseModel):
    """Class for HTTP headers (to avoid constantly fetching the entire pdf)."""

    last_modified: datetime.datetime
    content_length: int


class PDFPage(BaseModel):
    """Class for a PDF Page."""

    ocr: bool
    text: str


def get_store(hass: HomeAssistant, key: str) -> Store[PDF]:
    """Get a store."""
    return Store[PDF](hass, STORE_VERSION, f"{DOMAIN}_{key}")


class Progress:
    """Class to track progress of loading and processing the pdf."""

    def __init__(self) -> None:
        """Initialize progress."""
        self._progress_tasks: list[(float, float)] = []
        self._cur_task_index: int = 0

    def define_tasks(self, tasks: list[float]) -> None:
        """Add a progress task."""
        if 0 in tasks:
            raise ValueError("task estimates must be > 0")
        if not sum(tasks) == 1:
            raise ValueError("task estimate must sum to 1")
        if len(self._progress_tasks) == 0:
            self._progress_tasks = [(task, 0) for task in tasks]
            return
        # Normalize the task estimates so they add up to 1
        normalized_tasks: list[float] = [
            task * self._progress_tasks[self._cur_task_index][0] for task in tasks
        ]
        old_tasks = self._progress_tasks.copy()
        for i, pt in enumerate(
            old_tasks[self._cur_task_index :], start=self._cur_task_index
        ):
            if i == self._cur_task_index:
                for j, task in enumerate(normalized_tasks):
                    if i + j < len(self._progress_tasks):
                        self._progress_tasks[i + j] = (task, 0)
                    else:
                        self._progress_tasks.append((task, 0))
            elif i + len(tasks) < len(self._progress_tasks):
                self._progress_tasks[i + len(tasks)] = pt
            else:
                self._progress_tasks.append(pt)

    def clear_tasks(self) -> None:
        """Clear the task list."""
        self._progress_tasks = []
        self._cur_task_index = 0

    @property
    def progress(self) -> float:
        """Get the current progress."""
        return sum([cur for _, cur in self._progress_tasks])

    def advance_steps(self, tasks: int = 1) -> float:
        """Notify progress listeners."""
        for i in range(self._cur_task_index, self._cur_task_index + tasks):
            self._progress_tasks[i] = (
                self._progress_tasks[i][0],
                self._progress_tasks[i][0],
            )
        self._cur_task_index += tasks
        update: float = self.progress
        if update == 1:
            self.clear_tasks()
        _LOGGER.debug("Progress updated: %s", update)
        return update


class PDFScrape(ABC):
    """PDFScrape Base Class."""

    def __init__(self, hass: HomeAssistant, config_entry_id: str | None) -> None:
        """Called by classmethod with is called by the subclass."""
        self.hass: HomeAssistant = hass
        self._document: Document
        self.pdf: PDF = PDF()
        self.config_entry_id: str | None = config_entry_id
        self._stream: BytesIO
        self._progress = Progress()

        # if config_entry_id is None that means this a config flow and so just sends the file
        self.store: Store[PDF] | None = (
            get_store(hass, self.config_entry_id)
            if self.config_entry_id is not None
            else None
        )

    @property
    def progress(self) -> float:
        """Get the progress in progress."""
        return self._progress.progress

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
        tasks: list[float] = [0.05, 0.05, 0.65, 0.25]
        self._progress.define_tasks(tasks)
        self._document = await self.hass.async_add_executor_job(
            partial(Document, stream=self._stream)
        )
        self._progress.advance_steps()
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
        self._progress.advance_steps()
        # Check if there are changes, otherwise we should stop to save comp time
        if (
            self.pdf.loaded_from_store
            and modified == self.pdf.modified
            and sha256_checksum == self.pdf.sha256_checksum
        ):
            await self.close()
            self._progress.advance_steps(2)
            _LOGGER.debug("PDF not modified since last load, skipping processing")
            return False
        self.pdf.modified = modified
        self.pdf.sha256_checksum = sha256_checksum
        self.pdf.title = title
        self.pdf.page_count = self._document.page_count
        if self.store is not None:
            if len(self.pdf.pages) > 0:
                # already loaded pages, do we need to re-ocr them?
                await self._get_pages(set(self.pdf.pdf.pages.keys()), update=True)
            await self.store.async_save(self.pdf.model_dump())
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
            self._progress.advance_steps()
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

    async def _get_pages(
        self,
        page_nums: set[int],
        ocr: bool = False,
        update: bool = False,
    ) -> int:
        """Get txt on a pages."""
        progress_value: float = 1 / len(page_nums)
        self._progress.define_tasks(tasks=[progress_value] * len(page_nums))
        tasks: dict[int, Task] = {}
        async with TaskGroup() as tg:
            for page in page_nums:
                page_index = page - 1
                if page_index not in self.pdf.pages or (
                    ocr != self.pdf.pages[page_index].ocr
                ):
                    tasks[page_index] = tg.create_task(
                        self._get_page_text(page_index, ocr)
                    )
                elif update:
                    tasks[page_index] = tg.create_task(
                        self._get_page_text(page_index, self.pdf.pages[page_index].ocr)
                    )
        for page_index, task in tasks.items():
            if task.exception():
                _LOGGER.exception(
                    "Error processing page %s",
                    page_index + 1,
                    exc_info=task.exception(),
                )
            self.pdf.pages[page_index] = PDFPage(
                ocr=ocr or (update and self.pdf.pages[page_index].ocr),
                text=task.result(),
            )

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

    async def _get_page_text(self, page_index: int, ocr: bool = False) -> str:
        """Get text from a specific page."""
        await self._load_document_from_file_or_cache()
        if not ocr:
            text_page: TextPage = await self.hass.async_add_executor_job(
                self._document[page_index].get_textpage
            )
            return await self.hass.async_add_executor_job(text_page.extractText)
        return await self.hass.async_add_executor_job(
            partial(
                to_text,
                self._document,
                use_ocr=True,
                header=True,
                footer=True,
                pages=[page_index],
            )
        )

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
        return "\n".join(
            self.pdf.pages[page - 1].text or "" for page in sorted(page_nums)
        )


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
            self._progress.clear_tasks()
            self._progress.define_tasks([0.2, 0.8])
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
                    alt_modified = datetime.datetime.now(datetime.UTC)
                    alt_modified_source = ModifiedDateSource.FIRST_CHECK
            self._progress.advance_steps()
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
            return await self._process_pdf(
                (datetime.datetime.now(datetime.UTC), ModifiedDateSource.UPLOAD)
            )
        return False

    def __repr__(self):
        """Representation."""
        return f"PDF Uploaded - {self.pdf.title}" if self.pdf.title else "PDF Uploaded"


class PDFParseError(Exception):
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
