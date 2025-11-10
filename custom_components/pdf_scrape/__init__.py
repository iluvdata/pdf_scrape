"""PDF Scrape Integration."""

import logging

from homeassistant.components.hassio import async_get_clientsession

# import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN
from .coordinator import PDFScrapeConfigEntry, PDFScrapeCoordinator
from .pdf import HTTPError, PDFParseError, PDFScrape

_PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: PDFScrapeConfigEntry
) -> bool:
    """Set up the config entry."""
    try:
        pdf: PDFScrape = await PDFScrape.pdfscrape(
            config_entry.data[CONF_URL], async_get_clientsession(hass)
        )

        coordinator: PDFScrapeCoordinator = PDFScrapeCoordinator(
            hass, config_entry, pdf
        )

        await coordinator.async_config_entry_first_refresh()

        config_entry.async_on_unload(
            config_entry.add_update_listener(_async_update_listener)
        )

        config_entry.runtime_data = coordinator

        await hass.config_entries.async_forward_entry_setups(config_entry, _PLATFORMS)

    except PDFParseError as ex:
        _LOGGER.exception("Unable to parse_pdf: %s", config_entry.data[CONF_URL])
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="unable_to_parse",
            translation_placeholders={"exc": str(ex)},
        ) from ex

    except HTTPError as ex:
        _LOGGER.exception("Unable to access: %s", config_entry.data[CONF_URL])
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="http_error",
            translation_placeholders={"exc": str(ex)},
        ) from ex

    return True


async def _async_update_listener(
    hass: HomeAssistant, config_entry: PDFScrapeConfigEntry
):
    """Handle config options update."""
    # Reload the integration when the options change.
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: PDFScrapeConfigEntry,
    device_entry: DeviceEntry,
) -> bool:
    """Delete device if selected from UI."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
