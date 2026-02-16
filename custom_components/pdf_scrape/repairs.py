"""PDFScrape Repairs."""

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntriesFlowManager

from .const import HTTP_ERROR

from .config_flow import PDFScrapeConfigFlow


class HTTPRepairFlow(RepairsFlow):
    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            self.hass.config_entries.flow.async_init(
                DOMAIN,
            )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create flow."""
    match issue_id:
        case HTTP_ERROR:
            return HTTPRepairFlow(issue_id, data)
