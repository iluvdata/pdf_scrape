"""Config flow for PDF Scrape Integration."""

from collections.abc import Callable, Mapping
from datetime import timedelta
import logging
from logging import Logger
from pathlib import Path
import re
from typing import Any, cast

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    DEVICE_CLASS_UNITS,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.template import config_flow
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_ICON,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    CONF_TYPE,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_URL,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.config_validation import (
    isfile,
    matches_regex,
    path as pathcheck,
    url,
)
from homeassistant.helpers.entity import CalculatedState
from homeassistant.helpers.selector import (
    DurationSelector,
    DurationSelectorConfig,
    FileSelector,
    FileSelectorConfig,
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
    CONF_FILE,
    CONF_MIN_SCAN_INTERVAL,
    CONF_PDF_PAGES,
    CONF_REGEX_MATCH_INDEX,
    CONF_REGEX_SEARCH,
    CONF_VALUE_TEMPLATE,
    DOMAIN,
    REGEX_PAGE_RANGE_PATTERN,
    URL_FILE_INTEGRATION,
    ConfType,
)
from .pdf import (
    FileError,
    HTTPError,
    PDFParseError,
    PDFScrape,
    PDFScrapeHTTP,
    PDFScrapeUpload,
)

_LOGGER: Logger = logging.getLogger(__name__)


class PDFScrapeConfigFlow(ConfigFlow, domain=DOMAIN):
    """PDF Scrape Config Flow Class."""

    VERSION: int = 1
    MINOR_VERSION: int = 2

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
        return self.async_show_menu(
            step_id="user", menu_options=list(ConfType), sort=True
        )

    async def async_step_http(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle config flow."""
        errors: dict[str, str] = {}
        if user_input:
            td: timedelta = (
                timedelta(**user_input[CONF_SCAN_INTERVAL])
                if CONF_SCAN_INTERVAL in user_input
                else CONF_DEFAULT_SCAN_INTERVAL
            )
            if td < CONF_MIN_SCAN_INTERVAL:
                errors["base"] = "min_interval"
            else:
                try:
                    url(user_input[CONF_URL])
                    pdf: PDFScrapeHTTP = await PDFScrapeHTTP.pdfscrape(
                        self.hass, user_input[CONF_URL]
                    )
                    pdf.close()
                    # Store the token in the config entry data
                    title: str = user_input.get(CONF_NAME, user_input[CONF_URL])
                    data: dict[str, Any] = {
                        CONF_URL: user_input[CONF_URL],
                        CONF_SCAN_INTERVAL: {"seconds": td.total_seconds()},
                        CONF_TYPE: ConfType.HTTP,
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
                            title=title,
                            data=data,
                        )
                    _LOGGER.error("Accessed from invalid source: %s", self.source)
                    errors["base"] = "invalid_source"
                except vol.Invalid:
                    errors[CONF_URL] = "invalid_url"
                except PDFParseError:
                    errors[CONF_URL] = "pdf_parse"
                except FileError as err:
                    _LOGGER.warning("File/OS Error %s", err)
                    errors[CONF_FILE] = "file_error"
        flow_schema: vol.Schema = vol.Schema(
            {
                vol.Optional(CONF_NAME): TextSelector(),
                vol.Required(CONF_URL): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Required(CONF_SCAN_INTERVAL): DurationSelector(
                    DurationSelectorConfig(enable_day=False, allow_negative=False)
                ),
            }
        )
        if self.source == SOURCE_RECONFIGURE:
            data: dict[str, Any] = self._get_reconfigure_entry().data.copy()
            hours, remainder = divmod(data[CONF_SCAN_INTERVAL]["seconds"], 3600)
            minutes, seconds = divmod(remainder, 60)
            data[CONF_SCAN_INTERVAL] = {
                "hours": hours,
                "minutes": minutes,
                "seconds": seconds,
            }
            flow_schema = self.add_suggested_values_to_schema(flow_schema, data)
        else:
            hours, remainder = divmod(CONF_DEFAULT_SCAN_INTERVAL.total_seconds(), 3600)
            minutes, seconds = divmod(remainder, 60)
            flow_schema = self.add_suggested_values_to_schema(
                flow_schema,
                {
                    CONF_SCAN_INTERVAL: {
                        "hours": hours,
                        "minutes": minutes,
                        "seconds": seconds,
                    }
                },
            )
        return self.async_show_form(
            step_id="http",
            data_schema=flow_schema,
            errors=errors,
            description_placeholders={
                "min_int": str(CONF_MIN_SCAN_INTERVAL),
            },
        )

    async def async_step_upload(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle config flow."""
        errors: dict[str, str] = {}
        if user_input:
            try:
                with await self.hass.async_add_executor_job(
                    process_uploaded_file, self.hass, user_input[CONF_FILE]
                ) as pdf_path:
                    # Assign a flow_id for now as the file is deleted when we are done.
                    pdf: PDFScrapeUpload = await PDFScrapeUpload.pdfscrape(
                        self.hass, path=pdf_path, config_entry_id=self.flow_id
                    )
                    title: str = (
                        user_input.get(CONF_NAME) or pdf.metadata_name or "Uploaded PDF"
                    )
                    data: dict[str, Any] = {
                        "temp_storage_id": self.flow_id,
                        CONF_TYPE: ConfType.UPLOAD,
                    }
                    if self.source == SOURCE_USER:
                        await self.async_set_unique_id(
                            f"{DOMAIN}_{user_input[CONF_FILE]}"
                        )
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=title,
                            data=data,
                        )
                    if self.source == SOURCE_RECONFIGURE:
                        await self.async_set_unique_id(
                            f"{DOMAIN}_{user_input[CONF_FILE]}"
                        )
                        # self._abort_if_unique_id_mismatch()
                        return self.async_update_reload_and_abort(
                            self._get_reconfigure_entry(),
                            title=title,
                            data=data,
                        )
                _LOGGER.error("Accessed from invalid source: %s", self.source)
                errors["base"] = "invalid_source"
            except PDFParseError:
                errors[CONF_FILE] = "pdf_parse"
            except HTTPError as err:
                _LOGGER.warning("HTTP Error %s", err)
                errors[CONF_FILE] = "http_error"
        flow_schema: vol.Schema = vol.Schema(
            {
                vol.Optional(CONF_NAME): TextSelector(),
                vol.Required(CONF_FILE): FileSelector(
                    FileSelectorConfig(accept="application/pdf,.pdf")
                ),
            }
        )
        if self.source == SOURCE_RECONFIGURE:
            flow_schema = self.add_suggested_values_to_schema(
                flow_schema, self._get_reconfigure_entry().data
            )
        return self.async_show_form(
            data_schema=flow_schema,
            errors=errors,
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle config flow."""
        errors: dict[str, str] = {}
        error_placeholders: dict[str, str] = {}
        if user_input:
            try:
                await PDFScrapeUpload.pdfscrape(
                    self.hass, path=Path(isfile(pathcheck(user_input[CONF_FILE])))
                )
                data: dict[str, Any] = {
                    CONF_TYPE: ConfType.LOCAL,
                    CONF_FILE: user_input[CONF_FILE],
                }
                title: str = (
                    user_input.get(CONF_NAME) or user_input[CONF_FILE] or "Local PDF"
                )
                if self.source == SOURCE_USER:
                    await self.async_set_unique_id(f"{DOMAIN}_{user_input[CONF_FILE]}")
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=title,
                        data=data,
                    )
                if self.source == SOURCE_RECONFIGURE:
                    await self.async_set_unique_id(f"{DOMAIN}_{user_input[CONF_FILE]}")
                    # self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(),
                        title=title,
                        data=data,
                    )
                _LOGGER.error("Accessed from invalid source: %s", self.source)
                errors["base"] = "invalid_source"
            except PDFParseError:
                errors[CONF_FILE] = "pdf_parse"
            except (FileError, vol.Invalid) as err:
                _LOGGER.warning("File Error %s", err)
                if isinstance(err, vol.Invalid):
                    error_placeholders[CONF_FILE] = err.msg
                errors[CONF_FILE] = "file_error"
        flow_schema: vol.Schema = vol.Schema(
            {
                vol.Optional(CONF_NAME): TextSelector(),
                vol.Required(CONF_FILE): TextSelector(),
            }
        )
        if self.source == SOURCE_RECONFIGURE:
            flow_schema = self.add_suggested_values_to_schema(
                flow_schema, self._get_reconfigure_entry().data
            )
        return self.async_show_form(
            step_id="local",
            data_schema=flow_schema,
            errors=errors,
            description_placeholders={
                "url_file_integration": URL_FILE_INTEGRATION,
                **error_placeholders,
            },
        )

    async def async_step_reconfigure(self, user_input: None) -> ConfigFlowResult:
        """Handle reconf siguration."""
        match self._get_reconfigure_entry().data[CONF_TYPE]:
            case ConfType.HTTP:
                return await self.async_step_http()
            case ConfType.UPLOAD:
                return await self.async_step_upload()
        return await self.async_step_local()


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
        errors: dict[str, str] = {}

        config_entry: PDFScrapeConfigEntry = self._get_entry()

        if user_input:
            if not re.match(REGEX_PAGE_RANGE_PATTERN, user_input[CONF_PDF_PAGES]):
                errors[CONF_PDF_PAGES] = "invalid_page_range"
            try:
                config_entry.runtime_data.pdf.get_pages(user_input[CONF_PDF_PAGES])
            except ValueError:
                errors[CONF_PDF_PAGES] = "pages_out_of_range"
            if not errors:
                self.data[CONF_PDF_PAGES] = user_input[CONF_PDF_PAGES]
                return await self.async_step_regex(None)

        default_pages: str = "1"
        if self.source == SOURCE_RECONFIGURE:
            default_pages = str(
                self._get_reconfigure_subentry().data.get(CONF_PDF_PAGES, 0)
            )
        return self.async_show_form(
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PDF_PAGES, default=default_pages): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    ),
                }
            ),
            description_placeholders={
                "title": self._get_entry().title,
                "pages": len(config_entry.runtime_data.pdf.pages),
            },
            errors=errors,
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
        errors: dict[str, str] = {}
        pdf: PDFScrape = self._get_entry().runtime_data.pdf
        text: str = pdf.get_pages(self.data[CONF_PDF_PAGES])

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

        schema: vol.Schema = vol.Schema(
            {
                vol.Optional(CONF_REGEX_SEARCH): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Required("page_text", default=text): TextSelector(
                    TextSelectorConfig(multiline=True, read_only=True)
                ),
            }
        )
        if self.source == SOURCE_RECONFIGURE:
            schema = self.add_suggested_values_to_schema(
                schema, self._get_reconfigure_subentry().data
            )
        return self.async_show_form(
            step_id="regex",
            data_schema=schema,
            description_placeholders={
                "title": self._get_entry().title,
                CONF_PDF_PAGES: self.data[CONF_PDF_PAGES],
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

        pdf: PDFScrape = self._get_entry().runtime_data.pdf
        text: str = pdf.get_pages(self.data[CONF_PDF_PAGES])

        pattern: str | None = self.data.get(CONF_REGEX_SEARCH)
        matches: list[str] = re.findall(pattern, text) if pattern else []

        if user_input:
            value: str | list[str]
            if matches and user_input.get(CONF_REGEX_MATCH_INDEX) != "-1":
                value = matches[int(user_input[CONF_REGEX_MATCH_INDEX])]
            elif matches:
                value = matches
            else:
                value = text
            preview: PreviewSensorEntity | None = None
            errors, preview = _validate_step_matches(
                self.hass, config=user_input, value=value
            )
            if preview:
                try:
                    preview._async_calculate_state()  # noqa: SLF001
                except ValueError as ex:
                    errors["base"] = str(ex)
                    if len(errors["base"]) > 255:
                        errors["base"] = errors["base"][:255] + " <truncated>"
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
                    title=user_input[CONF_NAME],
                    data=self.data | user_input,
                )

        opts: list[SelectOptionDict] = []

        if len(matches) > 0:
            opts = [
                SelectOptionDict({"value": str(i), "label": f"{i + 1}. {matches[i]}"})
                for i in range(len(matches))
            ]
            opts.insert(0, SelectOptionDict({"value": "-1", "label": "All Matches"}))

        step_schema = {vol.Required(CONF_NAME): TextSelector()}

        if opts:
            step_schema = step_schema | {
                vol.Required(CONF_REGEX_MATCH_INDEX): SelectSelector(
                    SelectSelectorConfig(options=opts, mode=SelectSelectorMode.DROPDOWN)
                )
            }

        step_schema = step_schema | {
            vol.Optional(CONF_VALUE_TEMPLATE): TemplateSelector(),
            vol.Optional(CONF_UNIT_OF_MEASUREMENT): SelectSelector(
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
            vol.Optional(CONF_DEVICE_CLASS): SelectSelector(
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
            vol.Optional(CONF_STATE_CLASS): SelectSelector(
                SelectSelectorConfig(
                    options=[cls.value for cls in SensorStateClass],
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=True,
                    translation_key="sensor_state_class",
                ),
            ),
        }
        if self.source == SOURCE_RECONFIGURE:
            schema: vol.Schema = self.add_suggested_values_to_schema(
                vol.Schema(step_schema), self._get_reconfigure_subentry().data
            )
        else:
            schema: vol.Schema = vol.Schema(step_schema)

        return self.async_show_form(
            step_id="matches",
            data_schema=schema,
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
    flow_status: SubentryFlowResult = hass.config_entries.subentries.async_get(
        msg["flow_id"]
    )
    # Step: i.e. user, regex, etc
    step: str | None = flow_status.get("step_id")
    config_entry: PDFScrapeConfigEntry = hass.config_entries.async_get_known_entry(
        flow_status["handler"][0]
    )
    if not config_entry:
        raise HomeAssistantError
    pdf: PDFScrape = config_entry.runtime_data.pdf

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
    value: str | None = None
    flow: TargetSubentryFlowHandler = cast(
        TargetSubentryFlowHandler,
        hass.config_entries.subentries._progress.get(msg["flow_id"]),  # noqa: SLF001
    )
    preview: PreviewSensorEntity | None = None
    if step in ["user", "reconfigure"]:
        user_input[CONF_NAME] = "Text"
        user_input[CONF_ICON] = "mdi:file-pdf-box"
        pages = user_input[CONF_PDF_PAGES]
        if re.fullmatch(REGEX_PAGE_RANGE_PATTERN, pages) is None:
            errors[CONF_PDF_PAGES] = "invalid_page_range"
        else:
            try:
                value = pdf.get_pages(pages)
            except ValueError:
                errors[CONF_PDF_PAGES] = "invalid_page_range"
    else:
        pages = flow.data[CONF_PDF_PAGES]
        value = pdf.get_pages(pages)
        if step == "regex":
            pattern: str | None = user_input.get(CONF_REGEX_SEARCH)
            if pattern:
                user_input[CONF_ICON] = "mdi:text-box-search-outline"
                try:
                    matches_regex(user_input[CONF_REGEX_SEARCH])
                    matches: list[str] = re.findall(
                        user_input[CONF_REGEX_SEARCH],
                        value,
                    )
                    if len(matches) > 1:
                        user_input[CONF_NAME] = f"{len(matches)} Matches"
                        value = " ^ ".join(matches)
                    elif len(matches) == 1:
                        value = matches[0]
                        user_input[CONF_NAME] = "1 Match"
                    else:
                        value = "No matches found."
                        user_input[CONF_NAME] = "?"
                except re.PatternError as ex:
                    errors[CONF_REGEX_SEARCH] = str(ex.msg)
            else:
                user_input[CONF_ICON] = "mdi:file-pdf-box"
                user_input[CONF_NAME] = "Text"
        elif step == "matches":
            # Generate preview
            regex: str | None = flow.data.get(CONF_REGEX_SEARCH)
            if regex:
                matches: list[str] = re.findall(
                    regex,
                    pdf.pages[page],
                )
                match_idx: int = int(user_input[CONF_REGEX_MATCH_INDEX])
                if match_idx >= 0:
                    value = matches[match_idx]
                else:
                    value = matches
                errors, preview = _validate_step_matches(
                    hass, config=user_input, value=value
                )
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

    if not preview:
        preview = PreviewSensorEntity(hass, config=user_input, value=value)

    connection.send_result(msg["id"])

    connection.subscriptions[msg["id"]] = preview.async_start_preview(
        async_preview_updated
    )


class PreviewSensorEntity(SensorEntity):
    """Preview entity for frontend."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any], value: str) -> None:
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
            else (value if len(value) < 255 else value[:251] + "  ***")
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
        errors: str | None = None
        try:
            calculated_state: CalculatedState = self._async_calculate_state()
            preview_callback(
                calculated_state.state, calculated_state.attributes, None, None
            )
        except ValueError as ex:
            errors = str(ex)
            if len(errors) > 255:
                errors = errors[:250] + " ***"
        if errors:
            preview_callback(None, None, None, errors)

        return self._call_on_remove_callbacks


def _validate_step_matches(
    hass: HomeAssistant, config: dict[str, Any], value: str | list[str]
) -> tuple[dict[str, str], PreviewSensorEntity | None]:
    """Validate the matches step."""
    errors: dict[str, str] = {}
    if not config.get(CONF_NAME):
        errors[CONF_NAME] = "Name is required"
    if value_temp := config.get(CONF_VALUE_TEMPLATE):
        try:
            val_tmp: Template = Template(value_temp, hass)
            variables: TemplateVarsType = {"value": value}
            value = val_tmp.async_render(variables=variables, parse_result=False)
        except vol.Invalid as ex:
            errors[CONF_VALUE_TEMPLATE] = str(ex.msg)
        except TemplateError as ex:
            errors[CONF_VALUE_TEMPLATE] = str(ex)
    elif isinstance(value, list):
        errors[CONF_VALUE_TEMPLATE] = "Template is required when using all matches"
    # Validate the unit of measurement
    try:
        config_flow._validate_unit(config)  # noqa: SLF001
    except vol.Invalid as ex:
        errors[CONF_UNIT_OF_MEASUREMENT] = str(ex.msg)
    # Validate the state class
    try:
        config_flow._validate_state_class(config)  # noqa: SLF001
    except vol.Invalid as ex:
        errors[CONF_STATE_CLASS] = str(ex.msg)
    if errors:
        return errors, None
    preview: PreviewSensorEntity = PreviewSensorEntity(hass, config=config, value=value)
    return errors, preview
