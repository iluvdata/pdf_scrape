"""Config flow for PDF Scrape Integration."""

from asyncio import Task
from collections.abc import Callable, Mapping
from datetime import timedelta
import logging
from pathlib import Path
import re
from typing import Any, cast

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN, SelectEntity
from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    DEVICE_CLASS_UNITS,
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.template import config_flow
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    FlowType,
    SubentryFlowContext,
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
from homeassistant.helpers.entity import CalculatedState, Entity
import homeassistant.helpers.issue_registry as ir
from homeassistant.helpers.selector import (
    BooleanSelector,
    BooleanSelectorConfig,
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

from . import PDFScrapeConfigEntry, async_cleanup
from .const import (
    CONF_DEFAULT_SCAN_INTERVAL,
    CONF_FILE,
    CONF_MIN_SCAN_INTERVAL,
    CONF_OCR,
    CONF_PDF_PAGES,
    CONF_REGEX_MATCH_INDEX,
    CONF_REGEX_SEARCH,
    CONF_VALUE_TEMPLATE,
    DOCUMENT_SUBENTRY,
    DOMAIN,
    REGEX_PAGE_RANGE_PATTERN,
    URL_FILE_INTEGRATION,
    ConfType,
    ErrorTypes,
)
from .pdf import (
    FileError,
    HTTPError,
    PDFParseError,
    PDFScrape,
    PDFScrapeFile,
    PDFScrapeHTTP,
    PDFScrapeUpload,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)


class PDFScrapeConfigFlow(ConfigFlow, domain=DOMAIN):
    """PDF Scrape Config Flow Class."""

    VERSION: int = 1
    MINOR_VERSION: int = 2

    data: dict[str, str | timedelta | None] = {}
    placeholders: dict[str, str] | None = None
    reason: str = "already_configured"
    pdf: PDFScrape
    title_fun: Callable[[], str] | None = None
    unique_fun: Callable[[], str]
    process_task: Task | None = None

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

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult | None:
        """Finish this flow."""
        await self.async_set_unique_id(self.unique_fun())
        if entry := self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, self.unique_id
        ):
            if self.source == SOURCE_USER or (
                self.source == SOURCE_RECONFIGURE
                and entry.entry_id != self._get_reconfigure_entry().entry_id
            ):
                return self.async_abort(
                    reason=self.reason,
                    description_placeholders=self.placeholders,
                )
        title: str = self.data[CONF_NAME] if not self.title_fun else self.title_fun()
        if self.source == SOURCE_USER:
            return self.async_create_entry(
                title=title,
                data=self.data,
                subentries=[DOCUMENT_SUBENTRY.as_dict()],
            )
        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            f"{ErrorTypes.PDF_ERROR}_{self._get_reconfigure_entry().entry_id}",
        )
        return self.async_update_and_abort(
            self._get_reconfigure_entry(),
            title=title,
            data=self.data,
        )

    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Next flow for create flow."""
        subentry_flow: SubentryFlowResult = (
            await self.hass.config_entries.subentries.async_init(
                (result["result"].entry_id, "target"),
                context=SubentryFlowContext(source=SOURCE_USER),
            )
        )
        result["next_flow"] = (
            FlowType.CONFIG_SUBENTRIES_FLOW,
            subentry_flow["flow_id"],
        )
        return result

    async def async_step_process_error(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle errors during processing."""
        await async_cleanup(self.hass, self.flow_id)
        return self.async_abort(
            reason=self.reason,
            description_placeholders=self.placeholders,
        )

    def _async_progress(self) -> ConfigFlowResult | str | None:
        if self.process_task is not None:
            if not self.process_task.done():
                self.async_update_progress(self.pdf.progress)
                return self.async_show_progress(
                    step_id=self.cur_step["step_id"],
                    progress_action="pdf_process",
                    progress_task=self.process_task,
                )
            if exception := self.process_task.exception():
                _LOGGER.debug("Progress task exception", exc_info=exception)
                self.placeholders["msg"] = str(exception)
                if isinstance(exception, PDFParseError):
                    self.reason = "pdf_parse"
                elif isinstance(exception, HTTPError):
                    self.reason = "http_error"
                elif isinstance(exception, FileError):
                    self.reason = "file_error"
                else:
                    self.reason = "exception"
                return self.async_show_progress_done(next_step_id="process_error")
            self.process_task = None
            return self.async_show_progress_done(next_step_id="finish")
        return None

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
                    self.data[CONF_URL] = user_input[CONF_URL]
                    self.data[CONF_SCAN_INTERVAL] = {"seconds": td.total_seconds()}
                    self.data[CONF_TYPE] = ConfType.HTTP
                    self.placeholders = {"url": user_input[CONF_URL]}
                    self.reason = "http_already_configured"
                    self.pdf = PDFScrapeHTTP(self.hass, user_input[CONF_URL])
                    self.title_fun = lambda: (
                        user_input.get(CONF_NAME)
                        or self.pdf.pdf.title
                        or user_input[CONF_URL]
                    )
                    self.unique_fun = lambda: self.data[CONF_URL]
                    if self.process_task is None:
                        self.process_task = self.hass.async_create_task(
                            self.pdf.update(), "pdfscrape_process"
                        )
                except vol.Invalid:
                    errors[CONF_URL] = "invalid_url"
        if not errors and (result := self._async_progress()):
            return result

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
            flow_schema = self.add_suggested_values_to_schema(
                flow_schema,
                {
                    **data,
                    CONF_NAME: self._get_reconfigure_entry().title,
                },
            )
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

            def _process_pdf() -> PDFScrapeUpload:
                with process_uploaded_file(
                    self.hass, user_input[CONF_FILE]
                ) as pdf_path:
                    # Assign a flow_id for now as the file is deleted when we are done.
                    pdf: PDFScrapeUpload = PDFScrapeUpload(
                        self.hass,
                        (
                            self.flow_id
                            if self.source == SOURCE_USER
                            else self._get_reconfigure_entry().entry_id
                        ),
                        pdf_path,
                    )
                return pdf

            self.pdf: PDFScrapeUpload = await self.hass.async_add_executor_job(
                _process_pdf
            )
            self.title_fun = lambda: (
                self.data.get(CONF_NAME) or self.pdf.pdf.title or "Uploaded PDF"
            )
            self.unique_fun = lambda: self.pdf.pdf.sha256_checksum
            self.reason = "upload_already_configured"
            self.data["temp_storage_id"] = self.flow_id
            self.data[CONF_TYPE] = ConfType.UPLOAD
            if self.process_task is None:
                self.process_task = self.hass.async_create_task(
                    self.pdf.update(), "pdfscrape_process"
                )

        if result := self._async_progress():
            return result

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
                flow_schema,
                {
                    **self._get_reconfigure_entry().data,
                    CONF_NAME: self._get_reconfigure_entry().title,
                },
            )
        return self.async_show_form(
            data_schema=flow_schema,
            errors=errors,
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle config flow."""
        if user_input:
            self.pdf = PDFScrapeFile(
                self.hass, path=Path(isfile(pathcheck(user_input[CONF_FILE])))
            )
            self.data[CONF_TYPE] = ConfType.LOCAL
            self.data[CONF_FILE] = user_input[CONF_FILE]
            self.title_fun = lambda: (
                user_input.get(CONF_NAME) or user_input[CONF_FILE] or "Local PDF"
            )
            self.unique_fun = lambda: self.data[CONF_FILE]
            if self.process_task is None:
                self.process_task = self.hass.async_create_task(
                    self.pdf.update(), "process_pdf"
                )
        if result := await self._async_progress():
            return result
        flow_schema: vol.Schema = vol.Schema(
            {
                vol.Optional(CONF_NAME): TextSelector(),
                vol.Required(CONF_FILE): TextSelector(),
            }
        )
        if self.source == SOURCE_RECONFIGURE:
            flow_schema = self.add_suggested_values_to_schema(
                flow_schema,
                {
                    **self._get_reconfigure_entry().data,
                    CONF_NAME: self._get_reconfigure_entry().title,
                },
            )
        elif user_input is not None:
            flow_schema = self.add_suggested_values_to_schema(flow_schema, user_input)
        return self.async_show_form(
            step_id="local",
            data_schema=flow_schema,
            description_placeholders={
                "url_file_integration": URL_FILE_INTEGRATION,
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

    pdf: PDFScrape
    preview_task: Task[str] | None = None
    _progress_task: Task[str] | None = None

    def get_config_entry(self) -> PDFScrapeConfigEntry:
        """Return the config entry this subentry flow belongs to."""
        return cast(PDFScrapeConfigEntry, self._get_entry())

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

        if config_entry.state == ConfigEntryState.LOADED:
            self.pdf = config_entry.runtime_data.pdf
        else:  # Repair pathway?
            match config_entry.data[CONF_TYPE]:
                case ConfType.HTTP:
                    self.pdf = await PDFScrapeHTTP.pdfscrape(
                        self.hass,
                        config_entry.data[CONF_URL],
                        config_entry_id=config_entry.entry_id,
                    )
                case ConfType.LOCAL:
                    self.pdf = await PDFScrapeFile.pdfscrape(
                        self.hass,
                        config_entry.data[CONF_FILE],
                        config_entry_id=config_entry.entry_id,
                    )
                case ConfType.UPLOAD:
                    self.pdf = await PDFScrapeUpload.pdfscrape(
                        self.hass, config_entry_id=config_entry.entry_id
                    )

        if user_input:
            if not re.match(REGEX_PAGE_RANGE_PATTERN, user_input[CONF_PDF_PAGES]):
                errors[CONF_PDF_PAGES] = "invalid_page_range"
            self.data[CONF_PDF_PAGES] = user_input[CONF_PDF_PAGES]
            self.data[CONF_OCR] = user_input[CONF_OCR]

            self._progress_task = self.get_config_entry().async_create_task(
                self.hass,
                self.pdf.get_pages(
                    user_input[CONF_PDF_PAGES], user_input.get(CONF_OCR, False)
                ),
                "step_user_get_pages",
                True,
            )
        if self._progress_task is not None:
            if self._progress_task.done():
                if self._progress_task.exception():
                    if isinstance(self._progress_task.exception(), IndexError):
                        errors[CONF_PDF_PAGES] = "pages_out_of_range"
                    else:
                        errors["base"] = self._progress_task.exception()
                    self._progress_task = None
                else:
                    self._progress_task = None
                    return self.async_show_progress_done(next_step_id="select")
            else:
                return self.async_show_progress(
                    step_id="user",
                    progress_action="getting_pages",
                    progress_task=self._progress_task,
                )

        default_pages: str = "1"
        ocr: bool = False
        if self.source == SOURCE_RECONFIGURE:
            default_pages = str(
                self._get_reconfigure_subentry().data.get(CONF_PDF_PAGES, 0)
            )
            ocr = self._get_reconfigure_subentry().data.get(CONF_OCR, False)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PDF_PAGES, default=default_pages): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    ),
                    vol.Optional(CONF_OCR, default=ocr): BooleanSelector(
                        BooleanSelectorConfig(
                            read_only=True  # until we figure out tesseract app.
                        )
                    ),
                }
            ),
            description_placeholders={
                "title": self._get_entry().title,
                "pages": self.pdf.pdf.page_count,
            },
            errors=errors,
            last_step=False,
            preview="target",
        )

    @staticmethod
    async def async_setup_preview(hass: HomeAssistant) -> None:
        """Set up preview WS API."""
        websocket_api.async_register_command(hass, ws_start_preview)

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Get the regex."""
        errors: dict[str, str] = {}
        text: str = await self.pdf.get_pages(self.data[CONF_PDF_PAGES])

        if user_input and CONF_REGEX_SEARCH in user_input:
            # Validate that it's a valid regex
            try:
                matches: list[str] = re.findall(user_input[CONF_REGEX_SEARCH], text)
                # Do we get matches?
                if len(matches) > 0:
                    # Forward to sensor
                    self.data[CONF_REGEX_SEARCH] = user_input.get(CONF_REGEX_SEARCH)
                    return await self.async_step_sensor(None)
                errors[CONF_REGEX_SEARCH] = "no_matches"
            except re.PatternError as err:
                _LOGGER.warning("Invalid Regular Expression: %s", err.msg)
                errors[CONF_REGEX_SEARCH] = "bad_pattern"

        if user_input and user_input.get("page_text") and not errors:
            # User wants all the txt.
            return await self.async_step_sensor(None)

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
            step_id="select",
            data_schema=schema,
            description_placeholders={
                "title": self._get_entry().title,
                CONF_PDF_PAGES: self.data[CONF_PDF_PAGES],
            },
            last_step=False,
            errors=errors,
            preview="target",
        )

    async def async_step_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Get the regex."""
        errors: dict[str, str] = {}

        text: str = await self.pdf.get_pages(self.data[CONF_PDF_PAGES])

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
            errors, preview = _validate_step_sensor(
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
                    unique_ids: set[int] = {
                        int(unique_id)
                        for unique_id in {
                            subentry.unique_id
                            for subentry in self._get_entry().subentries.values()
                        }
                        if unique_id != "document"
                    }
                    return self.async_create_entry(
                        title=user_input.get(CONF_NAME),
                        data=self.data | user_input,
                        unique_id=str(
                            max(unique_ids) + 1 if len(unique_ids) > 0 else 0
                        ),
                    )
                # Was there an issue?
                iss_reg: ir.IssueRegistry = ir.async_get(self.hass)
                for error_type in ErrorTypes:
                    if issue := iss_reg.async_get_issue(
                        DOMAIN,
                        f"{error_type}_{self._get_entry().entry_id}_{self._get_reconfigure_subentry().subentry_id}",
                    ):
                        ir.async_delete_issue(self.hass, DOMAIN, issue.issue_id)
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
            step_id="sensor",
            data_schema=schema,
            last_step=True,
            errors=errors,
            preview="target",
        )

    def async_remove(self) -> None:
        """Handle removal of this subentry."""
        if self.preview_task is not None and not self.preview_task.done():
            self.preview_task.cancel()
        if self._progress_task is not None and not self._progress_task.done():
            self._progress_task.cancel()
        if self.pdf is not None:
            self.hass.add_job(self.pdf.close)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "target/start_preview",
        vol.Required("flow_id"): str,
        vol.Required("flow_type"): vol.Any("config_subentries_flow"),
        vol.Required("user_input"): dict,
    }
)
@websocket_api.async_response
async def ws_start_preview(
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
    # pdf: PDFScrape = config_entry.runtime_data.pdf

    errors: dict[str, str] = {}

    user_input: dict[str, Any] = msg["user_input"]

    @callback
    def async_preview_callback(
        state: str | None,
        attributes: Mapping[str, Any] | None,
        error: str | None,
        domain: str | None,
    ) -> None:
        """Forward updates to websocket."""
        if error is not None:
            connection.send_message(
                websocket_api.event_message(msg["id"], {"error": error})
            )
            return
        connection.send_message(
            websocket_api.event_message(
                msg["id"],
                {
                    "attributes": attributes,
                    "domain": domain,
                    "state": state,
                },
            )
        )

    value: str | list[str] | None = None
    flow: TargetSubentryFlowHandler = cast(
        TargetSubentryFlowHandler,
        hass.config_entries.subentries._progress.get(msg["flow_id"]),  # noqa: SLF001
    )
    pdf: PDFScrape = flow.pdf
    preview: PDFPreviewEntity | None = None
    if step in ["user", "reconfigure"]:
        user_input[CONF_NAME] = "Text"
        user_input[CONF_ICON] = "mdi:file-pdf-box"
        pages = user_input[CONF_PDF_PAGES]
        if re.fullmatch(REGEX_PAGE_RANGE_PATTERN, pages) is None:
            errors[CONF_PDF_PAGES] = "invalid_page_range"
        else:
            if flow.preview_task is not None and not flow.preview_task.done():
                flow.preview_task.cancel()
                await flow.preview_task
            flow.preview_task = flow.get_config_entry().async_create_task(
                hass,
                pdf.get_pages(pages, bool(user_input.get(CONF_OCR, False))),
                "pdf_preview_pages",
                True,
            )
            try:
                await flow.preview_task
            except IndexError:
                errors[CONF_PDF_PAGES] = "pages_out_of_range"
            if not errors:
                value = flow.preview_task.result()
    else:
        pages = flow.data[CONF_PDF_PAGES]
        value = await pdf.get_pages(pages)
        if step == "select":
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
                        matches = [
                            match if len(match) < 30 else match[:25] + " ***"
                            for match in matches
                        ]
                        preview = PreviewSelectEntity(
                            matches,
                            user_input,
                        )
                    elif len(matches) == 1:
                        value = matches[0]
                        user_input[CONF_NAME] = "1 Match"
                    else:
                        errors[CONF_REGEX_SEARCH] = "no_matches"
                except re.PatternError as ex:
                    errors[CONF_REGEX_SEARCH] = str(ex.msg)
            else:
                user_input[CONF_ICON] = "mdi:file-pdf-box"
                user_input[CONF_NAME] = "Text"
        elif step == "sensor":
            # Generate preview
            regex: str | None = flow.data.get(CONF_REGEX_SEARCH)
            if regex:
                matches: list[str] = re.findall(
                    regex,
                    await pdf.get_pages(pages),
                )
                match_idx: int = int(user_input[CONF_REGEX_MATCH_INDEX])
                if match_idx >= 0:
                    value = matches[match_idx]
                else:
                    value = matches
                errors, preview = _validate_step_sensor(
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

    if preview is None:
        preview = PreviewSensorEntity(value, user_input)

    connection.send_result(msg["id"])
    connection.subscriptions[msg["id"]] = preview.async_show_preview(
        async_preview_callback
    )


class PDFPreviewEntity(Entity):
    """Preview entity for frontend."""

    def __init__(self, config: dict[str, Any], domain: str) -> None:
        """Initialize a preview entity."""

        self._attr_name = config.get(CONF_NAME, "Preview")
        self._attr_icon = config.get(CONF_ICON, "mdi:eye")
        self.domain: str = domain
        self._preview_callback: (
            Callable[
                [str | None, Mapping[str, Any] | None, str | None, str | None],
                None,
            ]
            | None
        ) = None

    @callback
    def async_show_preview(
        self,
        preview_callback: Callable[
            [
                str | None,  # state
                Mapping[str, Any] | None,  # attributes
                str | None,  # errors
                str | None,  # domain
            ],
            None,
        ],
    ) -> CALLBACK_TYPE:
        """Start a preview."""
        error: str | None = None
        try:
            calculated_state: CalculatedState = self._async_calculate_state()
            preview_callback(
                calculated_state.state,
                calculated_state.attributes,
                None,
                self.domain,
            )
        except ValueError as ex:
            error = str(ex)
            if len(error) > 255:
                error = error[:250] + " ***"
        if error:
            preview_callback(None, None, error, None)
        return self._call_on_remove_callbacks


class PreviewSensorEntity(PDFPreviewEntity, SensorEntity):
    """Preview sensor entity for frontend."""

    def __init__(
        self, value: str, config: dict[str, Any], domain: str | None = None
    ) -> None:
        """Initialize a preview entity."""
        super().__init__(config, SENSOR_DOMAIN)
        self.domain: str | None = domain
        self._attr_device_class = config.get(CONF_DEVICE_CLASS)
        self._attr_native_unit_of_measurement = config.get(CONF_UNIT_OF_MEASUREMENT)
        self._attr_state_class = config.get(CONF_STATE_CLASS)
        self._attr_native_value = value if len(value) < 255 else value[:251] + "  ***"


class PreviewSelectEntity(PDFPreviewEntity, SelectEntity):
    """Preview sensor entity for frontend."""

    def __init__(
        self,
        options: list[str],
        config: dict[str, Any],
    ) -> None:
        """Initialize a preview entity."""
        super().__init__(config, SELECT_DOMAIN)
        self._attr_options = options
        self._attr_current_option = options[0] if len(options) > 0 else None


def _validate_step_sensor(
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
    preview: PreviewSensorEntity = PreviewSensorEntity(value=value, config=config)
    return errors, preview
