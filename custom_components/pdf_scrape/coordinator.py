"""Coordinator to download and parse pdf files."""

from datetime import datetime, timedelta
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, TemplateError
from homeassistant.helpers.template import Template, TemplateVarsType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_DEFAULT_SCAN_INTERVAL,
    CONF_MD5_CHECKSUM,
    CONF_MODIFIED,
    CONF_MODIFIED_SOURCE,
    CONF_PDF_PAGE,
    CONF_REGEX_MATCH_INDEX,
    CONF_REGEX_SEARCH,
    CONF_VALUE_TEMPLATE,
    DOMAIN,
)
from .pdf import ModifiedDateSource, PDFParseError, PDFScrape

_LOGGER: logging.Logger = logging.getLogger(__name__)

type PDFScrapeConfigEntry = ConfigEntry[PDFScrapeCoordinator]


class PDFScrapeCoordinator(DataUpdateCoordinator[dict[str, str]]):
    """Data coordinator to download and parse the files."""

    def __init__(
        self, hass: HomeAssistant, config_entry: PDFScrapeConfigEntry, pdf: PDFScrape
    ) -> None:
        """Initialize coordinator."""
        self.poll_interval: dict[str, int] = config_entry.data.get(
            CONF_SCAN_INTERVAL, {"seconds": CONF_DEFAULT_SCAN_INTERVAL}
        )
        self.config_entry: PDFScrapeConfigEntry
        super().__init__(
            hass,
            _LOGGER,
            name=f"PDF Scrape {pdf.url}",
            config_entry=config_entry,
            update_interval=(
                timedelta(**config_entry.data[CONF_SCAN_INTERVAL])
                if CONF_SCAN_INTERVAL in config_entry.data
                else CONF_DEFAULT_SCAN_INTERVAL
            ),
        )
        self.pdf: PDFScrape = pdf
        self.data = {}

    async def _async_update_data(self) -> dict[str, str]:
        """Perform the update."""
        try:
            await self.pdf.update()

        except PDFParseError as ex:
            _LOGGER.exception("Unable to parse_pdf: %s", self.pdf.url)
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="unable_to_parse",
                translation_placeholders={"exc": str(ex)},
            ) from ex
        config_updates: dict[str, Any] = {}
        if self.pdf.modified is None:
            if CONF_MD5_CHECKSUM not in self.config_entry.data:
                # first run
                config_updates[CONF_MODIFIED_SOURCE] = ModifiedDateSource.FIRST_CHECK
                config_updates[CONF_MODIFIED] = datetime.now()
            elif self.pdf.md5_checksum != self.config_entry.data[CONF_MD5_CHECKSUM]:
                # we have an updated pdf.
                config_updates[CONF_MODIFIED] = datetime.now()
                config_updates[CONF_MODIFIED_SOURCE] = ModifiedDateSource.CHECKSUM
        elif (
            CONF_MD5_CHECKSUM not in self.config_entry.data
            or self.pdf.md5_checksum != self.config_entry.data[CONF_MD5_CHECKSUM]
        ):
            # we have a date but the file has changed.
            config_updates[CONF_MODIFIED] = self.pdf.modified
            config_updates[CONF_MODIFIED_SOURCE] = self.pdf.modified_source
        if config_updates:
            config_updates[CONF_MD5_CHECKSUM] = self.pdf.md5_checksum
            prior_config = dict(self.config_entry.data)
            prior_config.update(config_updates)
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=prior_config
            )

        for subentry_conf_key in self.config_entry.subentries:
            subentry_conf: ConfigSubentry = self.config_entry.subentries[
                subentry_conf_key
            ]

            txt: str = ""
            try:
                txt = self.pdf.pages[int(subentry_conf.data[CONF_PDF_PAGE])]
            except IndexError as ex:
                _LOGGER.exception(
                    "Page %i not found in %s",
                    int(subentry_conf.data[CONF_PDF_PAGE]),
                    subentry_conf.title,
                )
                raise ConfigEntryError(
                    translation_domain=DOMAIN,
                    translation_key="index_error",
                    translation_placeholders={
                        "page": subentry_conf.data[CONF_PDF_PAGE],
                        "conf": subentry_conf.title,
                    },
                ) from ex
            if subentry_conf.data.get(CONF_REGEX_SEARCH):
                try:
                    matches: list[str] = re.findall(
                        subentry_conf.data[CONF_REGEX_SEARCH], txt
                    )
                    if not matches:
                        raise ConfigEntryError(
                            translation_domain=DOMAIN,
                            translation_key="no_matches",
                            translation_placeholders={
                                "regex": subentry_conf.data[CONF_REGEX_SEARCH],
                                "conf": subentry_conf.title,
                            },
                        )
                    txt = matches[int(subentry_conf.data[CONF_REGEX_MATCH_INDEX])]
                except re.PatternError as ex:
                    _LOGGER.exception(
                        "Pattern Error: %s using regex: %s on %s",
                        ex.msg,
                        subentry_conf.data[CONF_REGEX_SEARCH],
                        subentry_conf.title,
                    )
                    raise ConfigEntryError(
                        translation_domain=DOMAIN,
                        translation_key="pattern_error",
                        translation_placeholders={
                            "exc_msg": ex.msg,
                            "regex": subentry_conf.data[CONF_REGEX_SEARCH],
                            "conf": subentry_conf.title,
                        },
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
                    _LOGGER.exception(
                        "Template Error on %s",
                        subentry_conf.title,
                    )
                    raise ConfigEntryError(
                        translation_domain=DOMAIN,
                        translation_key="template_error",
                        translation_placeholders={
                            "exc_msg": str(ex),
                            "conf": subentry_conf.title,
                        },
                    ) from ex
            self.data[subentry_conf_key] = txt
        return self.data
