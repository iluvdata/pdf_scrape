"""View to load local pdfs."""

import os

from aiohttp import hdrs, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import KEY_AUTHENTICATED
from homeassistant.helpers.storage import STORAGE_DIR

from .const import DOMAIN, ConfType
from .coordinator import PDFScrapeConfigEntry


async def async_setup(hass: HomeAssistant) -> None:
    """Set up the PDF view."""
    hass.http.register_view(PDFView(hass))


class PDFView(HomeAssistantView):
    """PDF View."""

    name = "api:pdfscrape:pdf"
    url = "/api/pdf_scrape/pdf/{entry_id}.pdf"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize a PDFView."""
        self.hass = hass

    async def head(self, request: web.Request, entry_id: str) -> web.Response:
        """Head just for length and last-modified."""
        entry: PDFScrapeConfigEntry = self._get_entry_and_authenticatey(
            request, entry_id
        )
        path: str = self.hass.config.path(STORAGE_DIR, DOMAIN, f"{entry_id}.pdf")
        try:
            size: int = await self.hass.async_add_executor_job(os.path.getsize, path)
        except FileNotFoundError as exc:
            raise web.HTTPFound(reason="PDF is missing from file system") from exc
        return web.Response(
            content_type="application/pdf",
            content_length=size,
            last_modified=entry.runtime_data.pdf.pdf.modified,
        )

    async def get(self, request: web.Request, entry_id: str) -> web.FileResponse:
        """Serve the pdf."""
        entry: PDFScrapeConfigEntry = self._get_entry_and_authenticate(
            request, entry_id
        )
        response: web.FileResponse = web.FileResponse(
            self.hass.config.path(STORAGE_DIR, DOMAIN, f"{entry_id}.pdf")
        )
        response.last_modified = entry.runtime_data.pdf.pdf.modified
        return response

    def _get_entry_and_authenticate(
        self, request: web.Request, entry_id: str
    ) -> PDFScrapeConfigEntry:
        if entry := self.hass.config_entries.async_get_entry(entry_id):
            if entry.data["type"] == ConfType.HTTP:
                raise web.HTTPBadRequest(reason="Can't request for HTTP/HTTPS pdfs")
            authenticated = (
                request[KEY_AUTHENTICATED]
                or request.query.get("token") == entry.runtime_data.access_token
            )
            if not authenticated:
                # Attempt with invalid bearer token, raise unauthorized
                # so ban middleware can handle it.
                if hdrs.AUTHORIZATION in request.headers:
                    raise web.HTTPUnauthorized
                # Invalid sigAuth or image entity access token
                raise web.HTTPForbidden

            return entry
        raise web.HTTPNotFound(reason="PDF not found")
