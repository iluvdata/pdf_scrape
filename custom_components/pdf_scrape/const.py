"""Constants for PDF Scrape."""

from datetime import timedelta
from enum import StrEnum
from re import Pattern
from typing import Final

from homeassistant.config_entries import ConfigSubentry

CONF_DEFAULT_SCAN_INTERVAL: Final[timedelta] = timedelta(minutes=5)
CONF_MIN_SCAN_INTERVAL: Final[timedelta] = timedelta(minutes=1)
DOMAIN: Final[str] = "pdf_scrape"
CONF_PDF_PAGES: Final[str] = "pdf_pages"
CONF_REGEX_SEARCH: Final[str] = "regex_search"
CONF_REGEX_MATCH_INDEX: Final[str] = "regex_match_index"
CONF_VALUE_TEMPLATE: Final[str] = "value_template"
CONF_SHA256_CHECKSUM: Final = "sha256_checksum"
CONF_MODIFIED: Final[str] = "modified"
CONF_MODIFIED_SOURCE: Final[str] = "modified_source"
CONF_FILE: Final[str] = "file"
CONF_OCR: Final[str] = "ocr"


class ErrorTypes(StrEnum):
    """Error types for integration."""

    PDF_ERROR = "pdf_error"
    PATTERN_ERROR = "pattern_error"
    TEMPLATE_ERROR = "template_error"
    INDEX_ERROR = "index_error"
    NO_MATCHES = "no_matches"


REGEX_PAGE_RANGE_PATTERN: Final[Pattern] = r"^[\d]+(-[\d]+)?(,[\d]+(-[\d]+)?)*$"

URL_FILE_INTEGRATION: Final[str] = "https://www.home-assistant.io/integrations/file/"


class ConfType(StrEnum):
    """Conf types for integration."""

    HTTP = "http"
    UPLOAD = "upload"
    LOCAL = "local"


DOCUMENT_SUBENTRY: ConfigSubentry = ConfigSubentry(
    subentry_type="document", title="PDF", unique_id="document", data={}
)
