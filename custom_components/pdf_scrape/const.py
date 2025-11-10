"""Constants for PDF Scrape."""

from datetime import timedelta
from typing import Final

CONF_DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)
CONF_MIN_SCAN_INTERVAL: Final[timedelta] = timedelta(seconds=30)
DOMAIN: Final = "pdf_scrape"
CONF_PDF_PAGE: Final = "pdf_page"
CONF_REGEX_SEARCH: Final = "regex_search"
CONF_REGEX_MATCH_INDEX: Final = "regex_match_index"
CONF_VALUE_TEMPLATE: Final = "value_template"
CONF_MD5_CHECKSUM: Final = "md5_checksum"
CONF_MODIFIED: Final = "modified"
CONF_MODIFIED_SOURCE: Final = "modified_source"
