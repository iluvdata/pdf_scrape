"""PDFScrape Sensor."""

from datetime import datetime

from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    SensorDeviceClass,
    SensorEntity,
    cached_property,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_TYPE,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_URL,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PDFScrapeConfigEntry
from .const import (
    CONF_FILE,
    CONF_MODIFIED_SOURCE,
    CONF_SHA256_CHECKSUM,
    DOMAIN,
    ConfType,
)
from .coordinator import PDFScrapeCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PDFScrapeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up PDFScrape Entity from a subconfig entry."""
    coordinator: PDFScrapeCoordinator = config_entry.runtime_data

    for subentry_id, subentry in config_entry.subentries.items():
        if subentry.subentry_type == "document":
            async_add_entities(
                [PDFDocumentSensor(coordinator)], config_subentry_id=subentry_id
            )
            continue
        async_add_entities(
            [PDFScrapeSensor(coordinator, subentry)],
            config_subentry_id=subentry_id,
        )


def _async_get_device_info(config_entry: PDFScrapeConfigEntry) -> DeviceInfo:
    device_info: DeviceInfo = DeviceInfo(
        identifiers={(DOMAIN, config_entry.entry_id)},
        name=config_entry.title,
        entry_type=DeviceEntryType.SERVICE,
    )
    match config_entry.data[CONF_TYPE]:
        case ConfType.LOCAL:
            device_info["model"] = config_entry.data[CONF_FILE]
        case ConfType.HTTP:
            device_info["configuration_url"] = config_entry.data[CONF_URL]
            device_info["model"] = config_entry.data[CONF_URL]
        case ConfType.UPLOAD:
            device_info["model"] = config_entry.title
    return device_info


class PDFDocumentSensor(CoordinatorEntity[PDFScrapeCoordinator], SensorEntity):  # type: ignore[reportIncompatibleVariableOverride]
    """PDFDocument Sensor (to watch for changes)."""

    def __init__(self, coordinator: PDFScrapeCoordinator) -> None:
        """Initialize PDFDocument Sensor."""
        super().__init__(coordinator)
        self._attr_name = "Last Modified"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self.unique_id = f"{DOMAIN}_document_{self.coordinator.config_entry.entry_id}"
        self._attr_has_entity_name = True
        self._attr_device_info = _async_get_device_info(coordinator.config_entry)
        self._attr_icon = "mdi:update"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_translation_key = "modified"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @cached_property
    def native_value(self) -> datetime | None:
        """Return the state of the sensor."""
        return self.coordinator.pdf.pdf.modified

    @cached_property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return Extra Attributes."""
        return {
            CONF_MODIFIED_SOURCE: self.coordinator.pdf.pdf.modified_source,
            CONF_SHA256_CHECKSUM: self.coordinator.pdf.pdf.sha256_checksum,
        }


class PDFScrapeSensor(CoordinatorEntity[PDFScrapeCoordinator], SensorEntity):  # type: ignore[reportIncompatibleVariableOverride]
    """PDFScrape Sensor Entity."""

    def __init__(
        self, coordinator: PDFScrapeCoordinator, subentry: ConfigSubentry
    ) -> None:
        """Initialize PDFScrape Sensor."""
        super().__init__(coordinator)
        if coordinator.config_entry is None:
            raise ConfigEntryError("This should never be raised")
        self.subentry_id: str = subentry.subentry_id
        self._attr_name = subentry.title
        self._attr_has_entity_name = True
        self._attr_device_info = _async_get_device_info(coordinator.config_entry)
        self._attr_native_unit_of_measurement = subentry.data.get(
            CONF_UNIT_OF_MEASUREMENT
        )
        self._attr_state_class = subentry.data.get(CONF_STATE_CLASS)
        self._attr_device_class = subentry.data.get(CONF_DEVICE_CLASS)
        self._attr_icon = "mdi:file-pdf-box"
        self.unique_id = f"{DOMAIN}_target_{self.subentry_id}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @cached_property
    def native_value(self) -> str:
        """Return the state of the sensor."""
        value: str = self.coordinator.data[self.subentry_id]
        return value if len(value) < 255 else value[:242] + " <truncated>"
