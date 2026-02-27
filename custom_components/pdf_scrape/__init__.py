"""PDF Scrape Integration."""

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file

# import aiohttp
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
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_FILE,
    CONF_MD5_CHECKSUM,
    CONF_MODIFIED,
    CONF_MODIFIED_SOURCE,
    CONF_PDF_PAGES,
    DOMAIN,
    ConfType,
    ErrorTypes,
)
from .coordinator import (
    PDFScrapeConfigEntry,
    PDFScrapeCoordinator,
    PDFScrapeHTTPCoordinator,
    PDFScrapeLocalCoordinator,
    PDFScrapeUploadCoordinator,
    async_raise_error,
)
from .pdf import (
    HTTPError,
    PDFParseError,
    PDFScrapeHTTP,
    PDFScrapeLocal,
    PDFScrapeUpload,
    StoredFile,
    get_store,
)

_PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


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
                    if pdf.modified is not None:
                        return {
                            CONF_MODIFIED: pdf.modified.isoformat(),
                            CONF_MODIFIED_SOURCE: pdf.modified_source,
                            CONF_MD5_CHECKSUM: pdf.md5_checksum,
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
    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: PDFScrapeConfigEntry
) -> bool:
    """Set up the config entry."""
    try:
        coordinator: PDFScrapeCoordinator
        match config_entry.data[
            CONF_TYPE
        ]:  # Default to HTTP if type is not set (for legacy entries)
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
                            new_store: Store[StoredFile] = get_store(
                                hass, config_entry.entry_id
                            )
                            await new_store.async_save(data)
                            await temp_store.async_remove()
                            hass.config_entries.async_update_entry(
                                config_entry, data={"type": "upload"}
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
                pdflocal: PDFScrapeLocal = await PDFScrapeLocal.pdfscrape(
                    hass,
                    config_entry.data[CONF_FILE],
                    config_entry_id=config_entry.entry_id,
                )
                coordinator = PDFScrapeLocalCoordinator(hass, config_entry, pdflocal)

        await coordinator.async_config_entry_first_refresh()

        config_entry.async_on_unload(
            config_entry.add_update_listener(_async_update_listener)
        )

        config_entry.runtime_data = coordinator

        await hass.config_entries.async_forward_entry_setups(config_entry, _PLATFORMS)

    except (HTTPError, TimeoutError, PDFParseError) as ex:
        async_raise_error(
            hass=hass,
            error_key=ErrorTypes.PDF_ERROR,
            config_entry=config_entry,
            exception=ex,
        )

    return True


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: PDFScrapeConfigEntry
) -> bool:
    """Check for config migration."""

    if config_entry.version == 1 and config_entry.minor_version == 1:
        new_data: dict[str, Any] = {**config_entry.data}
        # Move data to store and remove from config_entry data
        data: StoredFile = {}
        for k, v in config_entry.data.items():
            if k in [CONF_MODIFIED, CONF_MODIFIED_SOURCE, CONF_MD5_CHECKSUM]:
                data[k] = v
                del new_data[k]
        store: Store[StoredFile] = get_store(hass, config_entry.entry_id)
        await store.async_save(data)
        new_data[CONF_TYPE] = ConfType.HTTP

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=1, minor_version=2
        )

        if config_entry.subentries:
            for subentry in config_entry.subentries.values():
                if pdf_page := subentry.data.get("page_page"):
                    new_data: dict[str, Any] = {**subentry.data}
                    new_data[CONF_PDF_PAGES] = pdf_page
                    new_data.pop("page_page")
                    hass.config_entries.async_update_subentry(
                        config_entry,
                        subentry,
                        data=new_data,
                    )

    return True


async def _async_update_listener(
    hass: HomeAssistant, config_entry: PDFScrapeConfigEntry
):
    """Handle config options update."""
    # Reload the integration when the options change.
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    # Delete the store
    if store := get_store(hass, entry.entry_id):
        await store.async_remove()
