"""PDFScrape Sensor."""

from typing import Any

from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    ENTITY_ID_FORMAT,
    SensorEntity,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PDFScrapeConfigEntry
from .coordinator import PDFScrapeCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PDFScrapeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up PDFScrape Entity from a subconfig entry."""
    coordinator: PDFScrapeCoordinator = config_entry.runtime_data.coordinator

    await coordinator.async_config_entry_first_refresh()

    for subentry_config_key in coordinator.data:
        entity: list[PDFScrapeSensor] = [
            PDFScrapeSensor(coordinator, subentry_config_key)
        ]
        async_add_entities(entity, config_subentry_id=subentry_config_key)


class PDFScrapeSensor(CoordinatorEntity, SensorEntity):
    """PDFScrape Sensor Entity."""

    def __init__(
        self, coordinator: PDFScrapeCoordinator, subentry_config_key: str
    ) -> None:
        """Initialize PDFScrape Sensor."""
        super().__init__(coordinator)

        self.subentry_config = coordinator.config_entry.subentries.get(
            subentry_config_key
        )
        if self.subentry_config is None:
            raise HomeAssistantError(
                f"Subentry config not found: {subentry_config_key}"
            )
        self._attr_name = self.subentry_config.title
        self._attr_native_unit_of_measurement = self.subentry_config.data.get(
            CONF_UNIT_OF_MEASUREMENT
        )
        self._attr_state_class = self.subentry_config.data.get(CONF_STATE_CLASS)
        self._attr_device_info = DeviceInfo(
            identifiers={
                (coordinator.config_entry.domain, coordinator.config_entry.entry_id)
            },
            name=coordinator.config_entry.title,
            manufacturer="PDFScrape",
            model="PDF Scrape Document",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._attr_device_class = self.subentry_config.data.get(CONF_DEVICE_CLASS)
        self._attr_icon = "mdi:file-pdf-box"
        self._attr_attribution = "PDFScrape"
        self.unique_id = f"pdfscrape{subentry_config_key}"
        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, self._attr_name, hass=self.coordinator.hass
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Return the current value of the sensor."""
        return self.coordinator.data[self.subentry_config.subentry_id]["txt"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra attributes for the state."""
        return {
            "last_modified": self.coordinator.data[self.subentry_config.subentry_id][
                "modified"
            ]
        }
