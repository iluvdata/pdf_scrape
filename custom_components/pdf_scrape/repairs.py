"""PDFScrape Repairs."""

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlowContext
from homeassistant.core import HomeAssistant

from .const import DOMAIN, HTTP_ERROR


class HTTPRepairFlow(RepairsFlow):
    """Repair for HTTP error."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Start reconfigure flow."""
        if user_input is not None:
            self.hass.config_entries.flow.async_init(
                DOMAIN,
                context=ConfigFlowContext(
                    source=SOURCE_RECONFIGURE, entry_id=self.data["entry_id"]
                ),
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
