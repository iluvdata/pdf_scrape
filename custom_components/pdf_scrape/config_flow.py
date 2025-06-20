"""Config flow for PDF Scrape Integration."""

from collections.abc import Callable, Mapping
from datetime import timedelta
import logging
from logging import Logger
import re
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    DEVICE_CLASS_UNITS,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
    StateType,
)

# pylint: disable-next=hass-component-root-import
from homeassistant.components.template.config_flow import (
    _validate_state_class,
    _validate_unit,
)
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_ICON,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_URL,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.config_validation import matches_regex, url
from homeassistant.helpers.entity import CalculatedState
from homeassistant.helpers.selector import (
    DurationSelector,
    DurationSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.template import Template, TemplateError, TemplateVarsType

from . import PDFScrapeConfigEntry
from .const import (
    CONF_DEFAULT_SCAN_INTERVAL,
    CONF_MIN_SCAN_INTERVAL,
    CONF_PDF_PAGE,
    CONF_REGEX_MATCH_INDEX,
    CONF_REGEX_SEARCH,
    CONF_VALUE_TEMPLATE,
    DOMAIN,
)
from .pdf import HTTPError, PDFParseError, PDFScrape

_LOGGER: Logger = logging.getLogger(__name__)


class PDFScrapeConfigFlow(ConfigFlow, domain=DOMAIN):
    """PDF Scrape Config Flow Class."""

    VERSION: int = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        return {"target": TargetSubentryFlowHandler}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle config flow."""
        return await self._async_configure(user_input)

    async def _async_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle config flow."""
        errors: dict[str, str] = {}
        if user_input:
            interval: int = timedelta(**user_input[CONF_SCAN_INTERVAL]).total_seconds()
            if interval < CONF_MIN_SCAN_INTERVAL:
                errors["base"] = "min_interval"
            else:
                try:
                    url(user_input[CONF_URL])
                    await PDFScrape.pdfscrape(user_input[CONF_URL])
                    # Store the token in the config entry data
                    title: str = (
                        user_input[CONF_NAME] + " - " + user_input[CONF_URL]
                        if user_input.get(CONF_NAME)
                        else user_input[CONF_URL]
                    )
                    data: dict[str, Any] = {
                        CONF_URL: user_input[CONF_URL],
                        CONF_SCAN_INTERVAL: interval,
                    }
                    if user_input.get(CONF_NAME):
                        data[CONF_NAME] = user_input[CONF_NAME]
                    if self.source == SOURCE_USER:
                        await self.async_set_unique_id(
                            f"{DOMAIN}_{user_input[CONF_URL]}"
                        )
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=title,
                            data=data,
                        )
                    if self.source == SOURCE_RECONFIGURE:
                        await self.async_set_unique_id(
                            f"{DOMAIN}_{user_input[CONF_URL]}"
                        )
                        # self._abort_if_unique_id_mismatch()
                        return self.async_update_reload_and_abort(
                            self._get_reconfigure_entry(),
                            data_updates=user_input,
                        )
                    _LOGGER.error("Accessed from invalid source: %s", self.source)
                    errors["base"] = "invalid_source"
                except vol.Invalid:
                    errors["base"] = "invalid_url"
                except PDFParseError:
                    errors["base"] = "pdf_parse"
                except HTTPError as err:
                    _LOGGER.warning("HTTP Error %s", err)
                    errors["base"] = "http_error"
        defaults: dict[str, str] = {}
        if self.source == SOURCE_RECONFIGURE:
            config_entry: ConfigEntry = self._get_reconfigure_entry()
            defaults[CONF_NAME] = config_entry.data.get(CONF_NAME)
            defaults[CONF_URL] = config_entry.data.get(CONF_URL)
            defaults[CONF_SCAN_INTERVAL] = config_entry.data.get(CONF_SCAN_INTERVAL)
        defaults[CONF_SCAN_INTERVAL] = defaults.get(
            CONF_SCAN_INTERVAL, CONF_DEFAULT_SCAN_INTERVAL
        )
        flow_schema: vol.Schema = vol.Schema(
            {
                vol.Optional(
                    CONF_NAME, default=defaults.get(CONF_NAME)
                ): TextSelector(),
                vol.Required(CONF_URL, default=defaults.get(CONF_URL)): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default={"seconds": defaults.get(CONF_SCAN_INTERVAL)},
                ): DurationSelector(
                    DurationSelectorConfig(enable_day=False, allow_negative=False)
                ),
            }
        )
        return self.async_show_form(
            data_schema=flow_schema,
            errors=errors,
            description_placeholders={
                "min_int": CONF_MIN_SCAN_INTERVAL,
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconf siguration."""
        return await self._async_configure(user_input)


class TargetSubentryFlowHandler(ConfigSubentryFlow):
    """Handle subentry flow for adding and modifying a target."""

    VERSION: int = 1

    data: dict[str, Any] = {}

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration."""
        return await self.async_step_user(user_input)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Get Page."""
        if user_input:
            self.data[CONF_PDF_PAGE] = int(user_input.get(CONF_PDF_PAGE))
            return await self.async_step_regex(None)

        pages: int = len(self._get_entry().runtime_data.coordinator.pdf.pages)
        opts: list[SelectOptionDict] = [
            SelectOptionDict({"value": str(i), "label": f"{i + 1}"})
            for i in range(pages)
        ]

        default_page: int = 0
        if self.source == SOURCE_RECONFIGURE:
            default_page = self._get_reconfigure_subentry().data.get(CONF_PDF_PAGE, 0)
        return self.async_show_form(
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PDF_PAGE, default=str(default_page)
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=opts, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            description_placeholders={
                "title": self._get_entry().title,
            },
            last_step=False,
            preview="target",
        )

    @staticmethod
    async def async_setup_preview(hass: HomeAssistant) -> None:
        """Set up preview WS API."""
        # try:
        websocket_api.async_register_command(hass, ws_start_preview)

    async def async_step_regex(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Get the regex."""
        errors: dict[str, str] = []
        pdf: PDFScrape = self._get_entry().runtime_data.coordinator.pdf
        pdf_page: int = self.data.get(CONF_PDF_PAGE)
        text: str = pdf.pages[pdf_page]

        if user_input and user_input.get(CONF_REGEX_SEARCH):
            # Validate that it's a valid regex
            try:
                matches: list[str] = re.findall(user_input[CONF_REGEX_SEARCH], text)
                # Do we get matches?
                if len(matches):
                    # Forward to matches
                    self.data[CONF_REGEX_SEARCH] = user_input.get(CONF_REGEX_SEARCH)
                    return await self.async_step_matches(None)
                errors["base"] = "no_matches"
            except re.PatternError as err:
                _LOGGER.warning("Invalid Regular Expression: %s", err.msg)
                errors["base"] = "bad_pattern"

        if user_input and user_input.get("page_text"):
            # User wants all the txt.
            return await self.async_step_matches(None)

        defaults: dict[str, str] = {}
        if self.source == SOURCE_RECONFIGURE:
            subentry_conf: ConfigSubentry = self._get_reconfigure_subentry()
            defaults[CONF_REGEX_SEARCH] = subentry_conf.data.get(CONF_REGEX_SEARCH)

        return self.async_show_form(
            step_id="regex",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_REGEX_SEARCH, default=defaults.get(CONF_REGEX_SEARCH)
                    ): TextSelector(TextSelectorConfig(multiline=True)),
                    vol.Required("page_text", default=text): TextSelector(
                        TextSelectorConfig(multiline=True)
                    ),
                }
            ),
            description_placeholders={
                "title": self._get_entry().title,
                CONF_PDF_PAGE: pdf_page + 1,
            },
            last_step=False,
            errors=errors,
            preview="target",
        )

    async def async_step_matches(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Get the regex."""
        errors: dict[str, str] = {}

        pdf: PDFScrape = self._get_entry().runtime_data.coordinator.pdf
        pdf_page: int = self.data.get(CONF_PDF_PAGE)
        text: str = pdf.pages[pdf_page]

        pattern: str = self.data.get(CONF_REGEX_SEARCH)
        matches: list[str] = re.findall(pattern, text) if pattern else []

        if user_input:
            if not user_input.get(CONF_NAME):
                errors[CONF_NAME] = "Name is required."
            if value_temp := user_input.get(CONF_VALUE_TEMPLATE):
                try:
                    val_tmp: Template = Template(value_temp, self.hass)
                    variables: TemplateVarsType = {
                        "value": matches[int(user_input[CONF_REGEX_MATCH_INDEX])]
                    }
                    val_tmp.async_render(
                        variables=variables, parse_result=False, limited=True
                    )
                except vol.Invalid as ex:
                    errors[CONF_VALUE_TEMPLATE] = str(ex.msg)
                except TemplateError as ex:
                    errors[CONF_VALUE_TEMPLATE] = str(ex.msg)
            try:
                _validate_unit(user_input)
            except vol.Invalid as ex:
                errors[CONF_UNIT_OF_MEASUREMENT] = str(ex.msg)
            try:
                _validate_state_class(user_input)
            except vol.Invalid as ex:
                errors[CONF_STATE_CLASS] = str(ex.msg)
            if not errors:
                if self.source != SOURCE_RECONFIGURE:
                    config_id: str = str(len(self._get_entry().subentries))
                    return self.async_create_entry(
                        title=user_input.get(CONF_NAME),
                        data=self.data | user_input,
                        unique_id=config_id,
                    )
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    title=user_input.get(CONF_NAME),
                    data_updates=self.data | user_input,
                )

        opts: list[SelectOptionDict] = None

        if len(matches) > 0:
            opts = [
                SelectOptionDict({"value": str(i), "label": f"{i + 1}. {matches[i]}"})
                for i in range(len(matches))
            ]

        defaults: dict[str, str] = {}
        if self.source == SOURCE_RECONFIGURE:
            subentry_conf: ConfigSubentry = self._get_reconfigure_subentry()
            defaults[CONF_NAME] = subentry_conf.data.get(CONF_NAME)
            defaults[CONF_REGEX_MATCH_INDEX] = subentry_conf.data.get(
                CONF_REGEX_MATCH_INDEX
            )
            defaults[CONF_VALUE_TEMPLATE] = subentry_conf.data.get(CONF_VALUE_TEMPLATE)
            defaults[CONF_STATE_CLASS] = subentry_conf.data.get(CONF_STATE_CLASS)
            defaults[CONF_UNIT_OF_MEASUREMENT] = subentry_conf.data.get(
                CONF_UNIT_OF_MEASUREMENT
            )
            defaults[CONF_DEVICE_CLASS] = subentry_conf.data.get(CONF_DEVICE_CLASS)

        step_schema = {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME)): TextSelector()
        }

        if opts:
            step_schema = step_schema | {
                vol.Required(
                    CONF_REGEX_MATCH_INDEX, default=defaults.get(CONF_REGEX_MATCH_INDEX)
                ): SelectSelector(
                    SelectSelectorConfig(options=opts, mode=SelectSelectorMode.DROPDOWN)
                )
            }
        step_schema = step_schema | {
            vol.Optional(
                CONF_VALUE_TEMPLATE, default=defaults.get(CONF_VALUE_TEMPLATE, "")
            ): TemplateSelector(),
            vol.Optional(
                CONF_UNIT_OF_MEASUREMENT, default=defaults.get(CONF_UNIT_OF_MEASUREMENT)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(
                        {
                            str(unit)
                            for units in DEVICE_CLASS_UNITS.values()
                            for unit in units
                            if unit is not None
                        }
                    ),
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                    sort=True,
                ),
            ),
            vol.Optional(
                CONF_DEVICE_CLASS, default=defaults.get(CONF_DEVICE_CLASS)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        cls.value
                        for cls in SensorDeviceClass
                        if cls != SensorDeviceClass.ENUM
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=True,
                    translation_key="sensor_device_class",
                ),
            ),
            vol.Optional(
                CONF_STATE_CLASS, default=defaults.get(CONF_STATE_CLASS)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[cls.value for cls in SensorStateClass],
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=True,
                    translation_key="sensor_state_class",
                ),
            ),
        }

        return self.async_show_form(
            step_id="matches",
            data_schema=vol.Schema(step_schema),
            last_step=True,
            errors=errors,
            preview="target",
        )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "target/start_preview",
        vol.Required("flow_id"): str,
        vol.Required("flow_type"): vol.Any("config_subentries_flow"),
        vol.Required("user_input"): dict,
    }
)
@callback
def ws_start_preview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Generate a preview."""
    # entity_registry_entry: er.RegistryEntry | None = None
    if msg["flow_type"] != "config_subentries_flow":
        raise HomeAssistantError("invalid_flow")
        # Get the config flow status
    flow_status = hass.config_entries.subentries.async_get(msg["flow_id"])
    # Step: i.e. user, regex, etc
    step = flow_status["step_id"]
    config_entry: PDFScrapeConfigEntry = hass.config_entries.async_get_known_entry(
        flow_status["handler"][0]
    )
    if not config_entry:
        raise HomeAssistantError
    pdf: PDFScrape = config_entry.runtime_data.coordinator.pdf

    errors: dict[str, str] = {}

    user_input: dict[str, Any] = msg["user_input"]

    @callback
    def async_preview_updated(
        state: str | None,
        attributes: Mapping[str, Any] | None,
        listeners: dict[str, bool | set[str]] | None,
        error: str | None,
    ) -> None:
        """Forward config entry state events to websocket."""
        if error is not None:
            connection.send_message(
                websocket_api.event_message(msg["id"], {"error": error})
            )
            return
        connection.send_message(
            websocket_api.event_message(
                msg["id"],
                {"attributes": attributes, "listeners": listeners, "state": state},
            )
        )

    page: int = 0
    value: str = ""
    flow: TargetSubentryFlowHandler = hass.config_entries.subentries._progress.get(  # noqa: SLF001
        msg["flow_id"]
    )
    if step in ["user", "reconfigure"]:
        user_input[CONF_NAME] = "Page Text"
        user_input[CONF_ICON] = "mdi:file-pdf-box"
        page = int(user_input[CONF_PDF_PAGE])
        value = pdf.pages[page - 1]
    else:
        page = flow.data.get(CONF_PDF_PAGE)
        if step == "regex":
            pattern: str | None = user_input.get(CONF_REGEX_SEARCH)
            if pattern:
                user_input[CONF_ICON] = "mdi:text-box-search-outline"
                try:
                    matches_regex(user_input[CONF_REGEX_SEARCH])
                    matches: list[str] = re.findall(
                        user_input[CONF_REGEX_SEARCH],
                        pdf.pages[page],
                    )
                    if len(matches):
                        value = ", ".join(
                            [
                                "{" + str(i + 1) + ": " + matches[i] + "}"
                                for i in range(len(matches))
                            ]
                        )
                        user_input[CONF_NAME] = f"{len(matches)} Matches"
                    else:
                        value = "No matches found."
                        user_input[CONF_NAME] = "?"
                except re.PatternError as ex:
                    errors[CONF_REGEX_SEARCH] = str(ex.msg)
            else:
                user_input[CONF_ICON] = "mdi:file-pdf-box"
                user_input[CONF_NAME] = "Page Text"
                value = pdf.pages[page]
        elif step == "matches":
            # Generate preview
            regex: str = flow.data.get(CONF_REGEX_SEARCH)
            if regex:
                matches: list[str] = re.findall(
                    regex,
                    pdf.pages[page],
                )
                value = matches[int(user_input.get(CONF_REGEX_MATCH_INDEX))]
            else:
                value = pdf.pages[page]
            if user_input.get(CONF_VALUE_TEMPLATE):
                try:
                    value_temp: Template = Template(
                        user_input.get(CONF_VALUE_TEMPLATE), hass
                    )
                    variables: TemplateVarsType = {"value": value}
                    value = value_temp.async_render(
                        variables=variables, parse_result=False, limited=True
                    )
                except vol.Invalid as ex:
                    errors[CONF_VALUE_TEMPLATE] = str(ex.msg)
            try:
                _validate_unit(user_input)
            except vol.Invalid as ex:
                errors[CONF_UNIT_OF_MEASUREMENT] = str(ex.msg)
            try:
                _validate_state_class(user_input)
            except vol.Invalid as ex:
                errors[CONF_STATE_CLASS] = str(ex.msg)
        else:
            return

    if errors:
        connection.send_message(
            {
                "id": msg["id"],
                "type": websocket_api.TYPE_RESULT,
                "success": False,
                "error": {"code": "invalid_user_input", "message": errors},
            }
        )
        return

    preview_entity: PreviewEntity = PreviewEntity(hass, config=user_input, value=value)

    connection.send_result(msg["id"])

    connection.subscriptions[msg["id"]] = preview_entity.async_start_preview(
        async_preview_updated
    )


class PreviewEntity(SensorEntity):
    """Preview entity for frontend."""

    def __init__(
        self, hass: HomeAssistant, config: dict[str, Any] | None, value: StateType
    ) -> None:
        """Initialize a preview entity."""

        self.hass: HomeAssistant = hass
        self._attr_name = config.get(CONF_NAME, "Preview")
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._attr_native_unit_of_measurement = config.get(CONF_UNIT_OF_MEASUREMENT)
        self._attr_state_class = config.get(CONF_STATE_CLASS)
        self._attr_icon = config.get(CONF_ICON, "mdi:eye")
        self._attr_native_value = (
            value
            if not isinstance(value, str)
            else (value if len(value) < 255 else value[:255] + " ***truncated***")
        )

    @callback
    def async_start_preview(
        self,
        preview_callback: Callable[
            [
                str | None,
                Mapping[str, Any] | None,
                dict[str, bool | set[str]] | None,
                str | None,
            ],
            None,
        ],
    ) -> CALLBACK_TYPE:
        """Render a preview."""

        calculated_state: CalculatedState = self._async_calculate_state()
        preview_callback(
            calculated_state.state, calculated_state.attributes, None, None
        )

        return self._call_on_remove_callbacks
