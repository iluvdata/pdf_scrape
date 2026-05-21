"""Tests for config, options, subentry flows and previews."""

import asyncio
import os

import pytest

from homeassistant import config_entries
from homeassistant.components.pdf_scrape import ConfType
from homeassistant.components.pdf_scrape.const import (
    CONF_FILE,
    CONF_PDF_PAGES,
    DOMAIN,
    CONF_OCR,
)
from homeassistant.const import CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from tests.typing import ClientSessionGenerator, WebSocketGenerator


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Temp dir for config."""
    return hass_tmp_config_dir


async def test_user_flow_upload(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test user initiated config flow with file upload."""

    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "file_upload", {})

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context=config_entries.ConfigFlowContext(source=config_entries.SOURCE_USER),
    )
    assert result.get(CONF_TYPE) is FlowResultType.MENU
    assert result.get("step_id") == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "upload"}
    )

    assert result.get(CONF_TYPE) is FlowResultType.FORM
    assert result.get("step_id") == "upload"

    client = await hass_client()

    with open("tests/components/pdf_scrape/sample.pdf", "rb") as sample_pdf:
        os.mkdir(os.path.join(hass.config.path(), ".storage"))

        async with client.post(
            "/api/file_upload", data={"file": sample_pdf}
        ) as response:
            assert response.status == 200
            data = await response.json()

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_FILE: data["file_id"]}
        )

        assert result[CONF_TYPE] is FlowResultType.SHOW_PROGRESS
        assert result["step_id"] == "upload"
        assert result["progress_action"] == "pdf_process"

        while result[CONF_TYPE] is FlowResultType.SHOW_PROGRESS:
            await asyncio.sleep(0.1)
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result[CONF_TYPE] is FlowResultType.CREATE_ENTRY
    assert result["subentries"][0]["subentry_type"] == "document"
    assert result["result"].data[CONF_TYPE] == ConfType.UPLOAD
    next_flow_type, next_flow_id = result["next_flow"]
    assert next_flow_type == config_entries.FlowType.CONFIG_SUBENTRIES_FLOW

    # test subentry flow

    result = await hass.config_entries.subentries.async_configure(next_flow_id)

    assert result[CONF_TYPE] is FlowResultType.FORM
    assert "target" in result["handler"]
    assert result["description_placeholders"]["pages"] == 4

    # test preview
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "target/start_preview",
            "flow_id": next_flow_id,
            "flow_type": config_entries.FlowType.CONFIG_SUBENTRIES_FLOW,
            "user_input": {CONF_PDF_PAGES: "2"},
        }
    )

    ws_response = await ws_client.receive_json()
    assert ws_response["id"] == 1
    assert ws_response["success"]

    ws_response = await ws_client.receive_json()

    assert ws_response["id"] == 1
    assert (
        "1\nFoo\nHello, here is some text without a meaning."
        in ws_response["event"]["state"]
    )

    result = await hass.config_entries.subentries.async_configure(
        next_flow_id, user_input={CONF_PDF_PAGES: "2", CONF_OCR: True}
    )

    assert result[CONF_TYPE] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "getting_pages"

    while result[CONF_TYPE] is FlowResultType.SHOW_PROGRESS:
        await asyncio.sleep(0.1)
        result = await hass.config_entries.subentries.async_configure(next_flow_id)

    assert result[CONF_TYPE] is FlowResultType.FORM
    assert result["step_id"] == "regex"

    assert (
        "1 Foo \n\nHello, here is some text without a meaning."
        in result["data_schema"]({})["page_text"]
    )

    # test preview with regex
    await ws_client.send_json(
        {
            "id": 2,
            "type": "target/start_preview",
            "flow_id": next_flow_id,
            "flow_type": config_entries.FlowType.CONFIG_SUBENTRIES_FLOW,
            "user_input": {"regex": r"This\stext"},
        }
    )

    ws_response = await ws_client.receive_json()
    assert ws_response["id"] == 2
    assert ws_response["success"]

    ws_response = await ws_client.receive_json()
    assert ws_response["id"] == 2

    response = await hass.config_entries.subentries.async_configure(
        next_flow_id, user_input={"regex": r"This text should show"}
    )
