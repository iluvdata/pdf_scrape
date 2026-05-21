"""Image entityfor PDFScape."""

from datetime import datetime
import logging
from pathlib import Path

from homeassistant.components.image import ImageEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PDFScrapeConfigEntry, PDFScrapeCoordinator
from .sensor import async_get_device_info

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PDFScrapeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up PDFScrape Entity from a subconfig entry."""
    document_subentry_id: str = [
        subentry.subentry_id
        for subentry in config_entry.subentries.values()
        if subentry.unique_id == "document"
    ][0]
    async_add_entities(
        [PDFImageEntity(config_entry.runtime_data)],
        config_subentry_id=document_subentry_id,
    )


class PDFImageEntity(ImageEntity, CoordinatorEntity[PDFScrapeCoordinator]):
    """Image entity for PDFScrape."""

    def __init__(
        self,
        coordinator: PDFScrapeCoordinator,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator.hass)
        super(ImageEntity, self).__init__(coordinator)
        self.device_info = async_get_device_info(coordinator.config_entry)
        self.hass = coordinator.hass
        self.unique_id = f"{DOMAIN}_thumbnail_{self.coordinator.config_entry.entry_id}"
        self._attr_name = "Thumbnail"
        self.has_entity_name = True
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def image_last_updated(self) -> datetime | None:
        """Return the last updated time of the pdf."""
        return self.coordinator.pdf.pdf.modified

    def image(self) -> bytes | None:
        """Return the image."""
        try:
            with Path.open(
                self.coordinator.hass.config.path(
                    STORAGE_DIR,
                    DOMAIN,
                    f"{self.coordinator.config_entry.entry_id}.webp",
                ),
                "rb",
            ) as file:
                return file.read()
        except FileNotFoundError:
            _LOGGER.error(
                "Image file not found for entry %s",
                self.coordinator.config_entry.entry_id,
            )
            return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
