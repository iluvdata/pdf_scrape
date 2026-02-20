"""PDFScrape Repairs."""

from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlowContext,
    ConfigFlowResult,
    FlowType,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PDF_ERROR


class PDFRepairFlow(ConfirmRepairFlow):
    """Repair for HTTP/Parse error."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Start reconfigure flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Start reconfigure flow."""
        if user_input is not None:
            next_flow: ConfigFlowResult = (
                await self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context=ConfigFlowContext(
                        entry_id=self.data["entry_id"], source=SOURCE_RECONFIGURE
                    ),
                )
            )
            result: ConfigFlowResult = self.async_abort(reason="next_flow")
            result["next_flow"] = (FlowType.CONFIG_FLOW, next_flow["flow_id"])
            return result
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            last_step=True,
            description_placeholders={"exc": self.data["exc"]},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create flow."""
    if data["error_key"] == PDF_ERROR:
        return PDFRepairFlow()
