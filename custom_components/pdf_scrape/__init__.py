"""PDF Scrape Integration."""

from asyncio import TaskGroup
from collections.abc import Awaitable, Callable
from functools import partial
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DEVICE_ID,
    CONF_TYPE,
    CONF_URL,
    Platform,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
import homeassistant.helpers.device_registry as dr
from homeassistant.helpers.selector import FileSelector, FileSelectorConfig
from homeassistant.helpers.storage import STORAGE_DIR, Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_FILE,
    CONF_MODIFIED,
    CONF_MODIFIED_SOURCE,
    CONF_SHA256_CHECKSUM,
    DOCUMENT_SUBENTRY,
    DOMAIN,
    ConfType,
    ErrorTypes,
)
from .coordinator import (
    PDFScrapeConfigEntry,
    PDFScrapeCoordinator,
    PDFScrapeFileCoordinator,
    PDFScrapeHTTPCoordinator,
    PDFScrapeUploadCoordinator,
    async_raise_error,
)
from .pdf import (
    PDF,
    FileError,
    HTTPError,
    PDFParseError,
    PDFScrapeFile,
    PDFScrapeHTTP,
    PDFScrapeUpload,
    get_store,
)

_PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_run_in_loop[*_Ts, _T](
    hass: HomeAssistant, target: Callable[[*_Ts], _T], *args: *_Ts
) -> Awaitable[_T]:
    """Run a function in the executor."""
    return await hass.async_add_executor_job(target, *args)


async def async_setup(hass: HomeAssistant, config_type: ConfigType) -> bool:
    """Setup service."""

    async def upload_pdf(call: ServiceCall) -> ServiceResponse:
        """Upload pdf Service."""

        error: str = ""

        config_entry: PDFScrapeConfigEntry | None = None
        if config_entry_ids := call.data.get(ATTR_CONFIG_ENTRY_ID):
            config_entry = hass.config_entries.async_get_entry(config_entry_ids[0])
        if config_entry is None:
            if device_ids := call.data.get(ATTR_DEVICE_ID):
                if device := dr.async_get(call.hass).async_get(device_ids[0]):
                    if device.primary_config_entry is not None:
                        config_entry = hass.config_entries.async_get_entry(
                            device.primary_config_entry
                        )
        if config_entry is not None:
            with await hass.async_add_executor_job(
                process_uploaded_file, hass, call.data[CONF_FILE]
            ) as pdf_path:
                try:
                    pdf: PDFScrapeUpload = await PDFScrapeUpload.pdfscrape(
                        call.hass,
                        path=pdf_path,
                        config_entry_id=config_entry.entry_id,
                    )
                    # Reload the config entry to pick up the new file
                    hass.config_entries.async_schedule_reload(config_entry.entry_id)
                    if pdf.pdf.modified is not None:
                        return {
                            CONF_MODIFIED: pdf.pdf.modified.isoformat(),
                            CONF_MODIFIED_SOURCE: pdf.pdf.modified_source,
                            CONF_SHA256_CHECKSUM: pdf.pdf.md5_checksum,
                        }
                    error = "Unable to parse uploaded PDF"
                except PDFParseError as ex:
                    _LOGGER.exception()
                    error = f"Unable to parse uploaded PDF {ex}"
        else:
            error = "Invalid config_entry_id or device_id"
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="pdf_not_uploaded",
            translation_placeholders={"error": error},
        )

    def _only_one(data: list[str]) -> list[str]:
        if len(data) != 1:
            raise vol.Invalid("Only one device/configuration entry can be specified.")
        return data

    def _one_of(data: dict[str, Any]) -> dict[str, Any]:
        if data.get(ATTR_DEVICE_ID) and data.get(ATTR_CONFIG_ENTRY_ID):
            raise vol.Invalid("Specify either device_id or config_entry_id, not both.")
        if not data.get(ATTR_DEVICE_ID) and not data.get(ATTR_CONFIG_ENTRY_ID):
            raise vol.Invalid("Either device_id or config_entry_id must be specified.")
        return data

    SCHEMA: vol.Schema = vol.Schema(
        vol.All(
            {
                vol.Optional(ATTR_CONFIG_ENTRY_ID): vol.All(cv.ensure_list, _only_one),
                vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, _only_one),
                vol.Required(CONF_FILE): FileSelector(
                    FileSelectorConfig(accept="application/pdf,.pdf")
                ),
            },
            _one_of,
        )
    )

    hass.services.async_register(
        DOMAIN,
        "upload_pdf",
        upload_pdf,
        SCHEMA,
        SupportsResponse.OPTIONAL,
    )

    # Clean up orphaned files if present
    # List of valid entries (and temp_ids)
    config_entry_ids: list[str] = [
        entry.entry_id
        if entry.data.get("temp_storage_id") is None
        else entry.data.get("temp_storage_id")
        for entry in hass.config_entries.async_entries(DOMAIN)
    ]
    path: Path = Path(hass.config.path(STORAGE_DIR))
    async with TaskGroup() as tg:
        for file in await hass.async_add_executor_job(path.iterdir):
            if file.is_file() and file.name.startswith(f"{DOMAIN}_"):
                entry_or_flow_id: str = file.name.removeprefix(f"{DOMAIN}_")
                if entry_or_flow_id not in config_entry_ids and (
                    store := get_store(hass, entry_or_flow_id)
                ):
                    _LOGGER.warning(
                        "Removing orphaned store: %s.  Note: This is not an error but indicates that the store is not associated with any config entry",
                        store.path,
                    )
                    tg.create_task(store.async_remove())
        path = path.joinpath(DOMAIN)
        if await hass.async_add_executor_job(path.exists):
            for file in await hass.async_add_executor_job(path.iterdir):
                if (
                    file.suffix in [".pdf", ".webp"]
                    and file.stem not in config_entry_ids
                ):
                    _LOGGER.warning(
                        "Removing orphaned file: %s.  Note: This is not an error but indicates that the file is not associated with any config entry",
                        file,
                    )
                    tg.create_task(async_run_in_loop(hass, file.unlink))

    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: PDFScrapeConfigEntry
) -> bool:
    """Set up the config entry."""
    if not config_entry.get_subentries_of_type(DOCUMENT_SUBENTRY.subentry_type):
        hass.config_entries.async_add_subentry(config_entry, DOCUMENT_SUBENTRY)
    try:
        coordinator: PDFScrapeCoordinator
        match config_entry.data[CONF_TYPE]:
            case ConfType.HTTP:
                pdfhttp: PDFScrapeHTTP = await PDFScrapeHTTP.pdfscrape(
                    hass,
                    config_entry.data[CONF_URL],
                    config_entry_id=config_entry.entry_id,
                )
                coordinator = PDFScrapeHTTPCoordinator(hass, config_entry, pdfhttp)
            case ConfType.UPLOAD:
                if temp_key := config_entry.data.get("temp_storage_id"):
                    # Rename the storage file that was created by the config flow (one time only)
                    if temp_store := get_store(hass, temp_key):
                        if data := await temp_store.async_load():
                            new_store: Store[PDF] = get_store(
                                hass, config_entry.entry_id
                            )
                            await new_store.async_save(data)
                            await temp_store.async_remove()
                            hass.config_entries.async_update_entry(
                                config_entry, data={"type": ConfType.UPLOAD}
                            )
                            # rename the pdf file.
                            path: Path = Path(hass.config.path(STORAGE_DIR), DOMAIN)
                            async with TaskGroup() as tg:
                                tg.create_task(
                                    async_run_in_loop(
                                        hass,
                                        partial(
                                            path.joinpath(f"{temp_key}.pdf").rename,
                                            path.joinpath(
                                                f"{config_entry.entry_id}.pdf"
                                            ),
                                        ),
                                    )
                                )
                                tg.create_task(
                                    async_run_in_loop(
                                        hass,
                                        partial(
                                            path.joinpath(f"{temp_key}.webp").rename,
                                            path.joinpath(
                                                f"{config_entry.entry_id}.webp"
                                            ),
                                        ),
                                    )
                                )
                        else:
                            raise ConfigEntryError("Temp store empty")
                    else:
                        raise ConfigEntryError("Unable to open temp store")

                pdfupload: PDFScrapeUpload = await PDFScrapeUpload.pdfscrape(
                    hass, config_entry_id=config_entry.entry_id
                )
                coordinator = PDFScrapeUploadCoordinator(hass, config_entry, pdfupload)
            case ConfType.LOCAL:
                pdflocal: PDFScrapeFile = await PDFScrapeFile.pdfscrape(
                    hass,
                    config_entry.data[CONF_FILE],
                    config_entry_id=config_entry.entry_id,
                )
                coordinator = PDFScrapeFileCoordinator(hass, config_entry, pdflocal)

        await coordinator.async_config_entry_first_refresh()

        config_entry.runtime_data = coordinator

        await hass.config_entries.async_forward_entry_setups(config_entry, _PLATFORMS)

    except (HTTPError, TimeoutError, PDFParseError, FileError) as ex:
        async_raise_error(
            hass=hass,
            error_key=ErrorTypes.PDF_ERROR,
            config_entry=config_entry,
            exception=ex,
        )

    config_entry.async_on_unload(config_entry.add_update_listener(update_listener))

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Handle pre-device delete checks."""
    # TODO: Tesseract device:  Also delete config_entry


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry):
    """Process update (when subentries are added)."""
    hass.config_entries.async_schedule_reload(config_entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    # Delete the store
    await async_cleanup(hass, entry.entry_id)


async def async_cleanup(hass: HomeAssistant, entry_or_flow_id: str) -> None:
    """Handle cleanup after removed entry and config flow error."""
    # Remove storage files
    path: Path = Path(hass.config.path(".storage", DOMAIN))
    async with TaskGroup() as tg:
        for file in async_run_in_loop(hass, path.glob(f"{entry_or_flow_id}.*")):
            tg.create_task(async_run_in_loop(hass, file.unlink))
        if store := get_store(hass, entry_or_flow_id):
            tg.create_task(store.async_remove())
