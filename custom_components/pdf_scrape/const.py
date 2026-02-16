"""Constants for PDF Scrape."""

from datetime import timedelta
from enum import StrEnum
from re import Pattern
from typing import Final

CONF_DEFAULT_SCAN_INTERVAL: Final[timedelta] = timedelta(minutes=5)
CONF_MIN_SCAN_INTERVAL: Final[timedelta] = timedelta(seconds=30)
DOMAIN: Final[str] = "pdf_scrape"
CONF_PDF_PAGES: Final[str] = "pdf_pages"
CONF_REGEX_SEARCH: Final[str] = "regex_search"
CONF_REGEX_MATCH_INDEX: Final[str] = "regex_match_index"
CONF_VALUE_TEMPLATE: Final[str] = "value_template"
CONF_MD5_CHECKSUM: Final = "md5_checksum"
CONF_MODIFIED: Final[str] = "modified"
CONF_MODIFIED_SOURCE: Final[str] = "modified_source"
CONF_FILE: Final[str] = "file"

HTTP_ERROR: Final[str] = "http_error"
PARSE_ERROR: Final[str] = "parse_error"
PATTERN_ERROR: Final[str] = "pattern_error"
TEMPLATE_ERROR: Final[str] = "template_error"
INDEX_ERROR: Final[str] = "index_error"

REGEX_PAGE_RANGE_PATTERN: Final[Pattern] = r"^[\d]+(-[\d]+)?(,[\d]+(-[\d]+)?)*$"

URL_FILE_INTEGRATION: Final[str] = "https://www.home-assistant.io/integrations/file/"


class ConfType(StrEnum):
    """Conf types for integration."""

    HTTP = "http"
    UPLOAD = "upload"
    LOCAL = "local"
