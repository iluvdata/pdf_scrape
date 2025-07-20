"""Coordinator to download and parse pdf files."""

from datetime import datetime, timedelta
import logging
import re
from typing import Any, TypedDict

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, TemplateError
from homeassistant.helpers.template import Template, TemplateVarsType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_DEFAULT_SCAN_INTERVAL,
    CONF_PDF_PAGE,
    CONF_REGEX_MATCH_INDEX,
    CONF_REGEX_SEARCH,
    CONF_VALUE_TEMPLATE,
)
from .pdf import PDFParseError, PDFScrape

_LOGGER: logging.Logger = logging.getLogger(__name__)


class PDFScrapeCoordinatorData(TypedDict):
    """Hold the data and date/time it was updated."""

    txt: str
    modified: datetime | None


class PDFScrapeCoordinator(DataUpdateCoordinator):
    """Data coordinator to download and parse the files."""

    data: dict[str, PDFScrapeCoordinatorData]

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, pdf: PDFScrape
    ) -> None:
        """Initialize coordinator."""
        self.poll_interval: dict[str, int] = config_entry.data.get(
            CONF_SCAN_INTERVAL, {"seconds": CONF_DEFAULT_SCAN_INTERVAL}
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"PDF Scrape {pdf.url}",
            update_interval=timedelta(**self.poll_interval),
            update_method=self.async_update_data,
        )
        self.pdf: PDFScrape = pdf
        self.config_entry: ConfigEntry = config_entry

    async def async_update_data(self) -> dict[str, Any]:
        """Perform the update."""
        data: dict[str, Any] = {}
        try:
            await self.pdf.update()
        except PDFParseError as ex:
            raise ConfigEntryError(
                f"Unable to parse pdf: {self.pdf.url}. Error: {ex}"
            ) from ex
        for subentry_conf_key in self.config_entry.subentries:
            subentry_conf: ConfigSubentry = self.config_entry.subentries[
                subentry_conf_key
            ]

            txt: str = ""
            try:
                txt = self.pdf.pages[int(subentry_conf.data[CONF_PDF_PAGE])]
            except IndexError as ex:
                raise ConfigEntryError(
                    f"Page {subentry_conf.data.get(CONF_PDF_PAGE)} not found in pdf for configuration {subentry_conf.title}"
                ) from ex
            if subentry_conf.data.get(CONF_REGEX_SEARCH):
                try:
                    matches: list[str] = re.findall(
                        subentry_conf.data[CONF_REGEX_SEARCH], txt
                    )
                    if not matches:
                        raise ConfigEntryError(
                            f"No matches found using regex: {subentry_conf.data.get(CONF_REGEX_SEARCH)} for configuration {subentry_conf.title}"
                        )
                    txt = matches[int(subentry_conf.data[CONF_REGEX_MATCH_INDEX])]
                except re.PatternError as ex:
                    raise ConfigEntryError(
                        f"{ex.msg}: {subentry_conf.data.get(CONF_REGEX_SEARCH)} for configuration {subentry_conf.title}"
                    ) from ex
            if subentry_conf.data.get(CONF_VALUE_TEMPLATE):
                val_tmp: Template = Template(
                    subentry_conf.data[CONF_VALUE_TEMPLATE], self.hass
                )
                variables: TemplateVarsType = {"value": txt}
                try:
                    txt = val_tmp.async_render(
                        variables=variables, parse_result=False, limited=True
                    )
                except TemplateError as ex:
                    raise ConfigEntryError(
                        f"Error rendering template: {ex} for configuration {subentry_conf.title}"
                    ) from ex
            data[subentry_conf_key] = PDFScrapeCoordinatorData(
                txt=txt, modified=self.pdf.modified
            )
        return data
