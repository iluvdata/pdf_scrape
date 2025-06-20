"""PDF Scrape Integration."""

from dataclasses import dataclass

# import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.device_registry import DeviceEntry

from .coordinator import PDFScrapeCoordinator
from .pdf import HTTPError, PDFParseError, PDFScrape

_PLATFORMS: list[Platform] = [Platform.SENSOR]

type PDFScrapeConfigEntry = ConfigEntry[RuntimeData]


@dataclass
class RuntimeData:
    """Class to hold data."""

    coordinator: PDFScrapeCoordinator


async def async_setup_entry(
    hass: HomeAssistant, config_entry: PDFScrapeConfigEntry
) -> bool:
    """Set up the config entry."""
    try:
        pdf: PDFScrape = await PDFScrape.pdfscrape(config_entry.data[CONF_URL])

        coordinator: PDFScrapeCoordinator = PDFScrapeCoordinator(
            hass, config_entry, pdf
        )

        await coordinator.async_config_entry_first_refresh()

        config_entry.async_on_unload(
            config_entry.add_update_listener(_async_update_listener)
        )

        config_entry.runtime_data = RuntimeData(coordinator)

        await hass.config_entries.async_forward_entry_setups(config_entry, _PLATFORMS)

    except PDFParseError as err:
        raise ConfigEntryError from err

    except HTTPError as err:
        raise ConfigEntryError(err.msg) from err

    return True


async def _async_update_listener(
    hass: HomeAssistant, config_entry: PDFScrapeConfigEntry
):
    """Handle config options update."""
    # Reload the integration when the options change.
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Delete device if selected from UI."""
    # Adding this function shows the delete device option in the UI.
    # Remove this function if you do not want that option.
    # You may need to do some checks here before allowing devices to be removed.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
