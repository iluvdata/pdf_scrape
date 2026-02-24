"""PDFScrape Repairs."""

from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlowContext,
    ConfigFlowResult,
    FlowType,
    SubentryFlowContext,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN, ErrorTypes


class PDFScrapeRepairFlow(RepairsFlow):
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
            return await self._async_get_next_flow()
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            last_step=True,
            description_placeholders={"msg": self.data["msg"]},
        )

    async def _async_get_next_flow(self) -> data_entry_flow.FlowResult:
        raise NotImplementedError("Must be implemented by subclasses.")


class PDFRepairFlow(PDFScrapeRepairFlow):
    """Repair for PDF errors."""

    async def _async_get_next_flow(self) -> ConfigFlowResult:
        next_flow: ConfigFlowResult = await self.hass.config_entries.flow.async_init(
            DOMAIN,
            context=ConfigFlowContext(
                entry_id=self.data["entry_id"], source=SOURCE_RECONFIGURE
            ),
        )
        return self.async_create_entry(
            data={}, next_flow=(FlowType.CONFIG_FLOW, next_flow["flow_id"])
        )


class TargetRepairFlow(PDFScrapeRepairFlow):
    """Repair for Target Errors."""

    async def _async_get_next_flow(self) -> SubentryFlowResult:
        next_flow: SubentryFlowResult = (
            await self.hass.config_entries.subentries.async_init(
                (self.data["entry_id"], "target"),
                context=SubentryFlowContext(
                    entry_id=self.data["entry_id"],
                    subentry_id=self.data["subentry_id"],
                    source=SOURCE_RECONFIGURE,
                ),
            )
        )
        return self.async_create_entry(
            data={},
            next_flow=(FlowType.CONFIG_SUBENTRIES_FLOW, next_flow["flow_id"]),
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create flow."""
    if data["error_key"] == ErrorTypes.PDF_ERROR:
        return PDFRepairFlow()
    if data["error_key"] in {
        ErrorTypes.PATTERN_ERROR,
        ErrorTypes.TEMPLATE_ERROR,
        ErrorTypes.INDEX_ERROR,
        ErrorTypes.NO_MATCHES,
    }:
        return TargetRepairFlow()
    raise NotImplementedError(f"Repair flow for {data['error_key']} not implemented.")
