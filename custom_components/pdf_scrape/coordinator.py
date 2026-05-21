"""Coordinator to download and parse pdf files."""

from datetime import timedelta
import logging
from random import SystemRandom
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, TemplateError
import homeassistant.helpers.issue_registry as ir
from homeassistant.helpers.template import Template, TemplateVarsType
from homeassistant.helpers.translation import async_get_exception_message
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DEFAULT_SCAN_INTERVAL,
    CONF_MIN_SCAN_INTERVAL,
    CONF_PDF_PAGES,
    CONF_REGEX_MATCH_INDEX,
    CONF_REGEX_SEARCH,
    CONF_VALUE_TEMPLATE,
    DOMAIN,
    ErrorTypes,
)
from .pdf import (
    HTTPError,
    PDFParseError,
    PDFScrape,
    PDFScrapeFile,
    PDFScrapeHTTP,
    PDFScrapeUpload,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)

type PDFScrapeConfigEntry = ConfigEntry[PDFScrapeCoordinator]


class PDFScrapeCoordinator(DataUpdateCoordinator[dict[str, str]]):
    """Data coordinator to download and parse the files."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: PDFScrapeConfigEntry,
        pdf: PDFScrape,
        update_inteval: timedelta | None = None,
    ) -> None:
        """Initialize coordinator."""
        self.config_entry: PDFScrapeConfigEntry
        super().__init__(
            hass,
            _LOGGER,
            name=f"PDF Scrape Coordinator {pdf}",
            config_entry=config_entry,
            update_interval=update_inteval,
            always_update=update_inteval is not None,
        )
        self.pdf: PDFScrape = pdf
        self.data = {}
        self.http_error_count: int = 0
        self.access_token: str = hex(SystemRandom().getrandbits(256))[2:]

    async def _async_update_data(self) -> dict[str, str]:
        """Perform the update."""
        try:
            if not self.data or await self.pdf.update():
                for subentry_key, subentry_conf in self.config_entry.subentries.items():
                    if subentry_conf.subentry_type == "document":
                        continue

                    txt: str = ""
                    try:
                        txt = await self.pdf.get_pages(
                            subentry_conf.data[CONF_PDF_PAGES]
                        )
                    except IndexError as ex:
                        async_raise_error(
                            hass=self.hass,
                            error_key=ErrorTypes.INDEX_ERROR,
                            config_entry=self.config_entry,
                            exception=ex,
                            translation_placeholders={
                                "pages": subentry_conf.data[CONF_PDF_PAGES]
                            },
                            config_subentry=subentry_conf,
                        )
                    if subentry_conf.data.get(CONF_REGEX_SEARCH):
                        try:
                            matches: list[str] = re.findall(
                                subentry_conf.data[CONF_REGEX_SEARCH], txt
                            )
                            if not matches:
                                async_raise_error(
                                    hass=self.hass,
                                    error_key="no_matches",
                                    config_entry=self.config_entry,
                                    translation_placeholders={
                                        "regex": subentry_conf.data[CONF_REGEX_SEARCH]
                                    },
                                    config_subentry=subentry_conf,
                                )
                            if subentry_conf.data[CONF_REGEX_MATCH_INDEX] != "-1":
                                txt = matches[
                                    int(subentry_conf.data[CONF_REGEX_MATCH_INDEX])
                                ]
                            else:
                                txt = matches
                        except re.PatternError as ex:
                            async_raise_error(
                                hass=self.hass,
                                error_key=ErrorTypes.PATTERN_ERROR,
                                config_entry=self.config_entry,
                                exception=ex,
                                translation_placeholders={
                                    "regex": subentry_conf.data[CONF_REGEX_SEARCH]
                                },
                                config_subentry=subentry_conf,
                            )
                    if subentry_conf.data.get(CONF_VALUE_TEMPLATE):
                        val_tmp: Template = Template(
                            subentry_conf.data[CONF_VALUE_TEMPLATE], self.hass
                        )
                        variables: TemplateVarsType = {"value": txt}
                        try:
                            txt = val_tmp.async_render(
                                variables=variables, parse_result=False
                            )
                        except TemplateError as ex:
                            async_raise_error(
                                self.hass,
                                error_key=ErrorTypes.TEMPLATE_ERROR,
                                config_entry=self.config_entry,
                                exception=ex,
                                config_subentry=subentry_conf,
                            )
                    self.data[subentry_key] = txt
        except (HTTPError, PDFParseError) as ex:
            if isinstance(ex, HTTPError) and self.http_error_count < 3:
                self.http_error_count += 1
                raise UpdateFailed(retry_after=30) from ex
            async_raise_error(
                hass=self.hass,
                error_key=ErrorTypes.PDF_ERROR,
                config_entry=self.config_entry,
                exception=ex,
                error_type=UpdateFailed,
            )
        self.http_error_count = 0

        return self.data


class PDFScrapeHTTPCoordinator(PDFScrapeCoordinator):
    """Data coordinator to download and parse the files."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: PDFScrapeConfigEntry,
        pdf: PDFScrapeHTTP,
    ) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            config_entry,
            pdf,
            timedelta(**config_entry.data[CONF_SCAN_INTERVAL])
            if CONF_SCAN_INTERVAL in config_entry.data
            else CONF_DEFAULT_SCAN_INTERVAL,
        )


class PDFScrapeUploadCoordinator(PDFScrapeCoordinator):
    """Data coordinator to download and parse the files."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: PDFScrapeConfigEntry,
        pdf: PDFScrapeUpload,
    ) -> None:
        """Initialize coordinator."""
        super().__init__(hass, config_entry, pdf)

    async def async_upload_pdf(self, pdf: PDFScrapeUpload) -> None:
        """Upload a new pdf."""
        self.pdf = pdf
        await self._async_update_data()


class PDFScrapeFileCoordinator(PDFScrapeCoordinator):
    """Data coordinator to download and parse the files."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: PDFScrapeConfigEntry,
        pdf: PDFScrapeFile,
    ) -> None:
        """Initialize coordinator."""
        super().__init__(hass, config_entry, pdf, CONF_MIN_SCAN_INTERVAL)


def async_raise_error(
    hass: HomeAssistant,
    error_key: str,
    config_entry: PDFScrapeConfigEntry,
    exception: Exception | None = None,
    translation_placeholders: dict[str, Any] | None = None,
    config_subentry: ConfigSubentry | None = None,
    error_type: ConfigEntryError | UpdateFailed = ConfigEntryError,
) -> None:
    """Log issues, create repairs, and raise exceptions."""

    if translation_placeholders is None:
        translation_placeholders = {}
    translation_placeholders["conf"] = (
        config_entry.title if config_subentry is None else config_subentry.title
    )
    msg: str = ""
    if exception is not None:
        msg = (
            str(exception)
            if not isinstance(exception, PDFParseError)
            else "Unable to parse pdf"
        )
    translation_placeholders["msg"] = msg
    data: dict[str, Any] = {
        "entry_id": config_entry.entry_id,
        "error_key": error_key,
        "msg": async_get_exception_message(DOMAIN, error_key, translation_placeholders),
    }
    if config_subentry is not None:
        data["subentry_id"] = config_subentry.subentry_id
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{error_key}_{config_entry.entry_id}{f'_{config_subentry.subentry_id}' if config_subentry is not None else ''}",
        data=data,
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=error_key,
        translation_placeholders=translation_placeholders,
    )
    if exception is not None:
        raise error_type(
            translation_domain=DOMAIN,
            translation_key=error_key,
            translation_placeholders=translation_placeholders,
        ) from exception
    raise error_type(
        translation_domain=DOMAIN,
        translation_key=error_key,
        translation_placeholders=translation_placeholders,
    )
