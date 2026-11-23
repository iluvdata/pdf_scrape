"""Regression tests for page indices when a cached local PDF changes."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pypdf import PdfReader, PdfWriter
import pytest

from custom_components.pdf_scrape.pdf import PDFScrapeFile, _PDFStore


@pytest.mark.parametrize("include_blank_page", [False, True])
@pytest.mark.asyncio
async def test_refresh_cached_pages(tmp_path: Path, include_blank_page: bool) -> None:
    """Refresh each retained page and discard negative and out-of-range indices."""
    source = tmp_path / "schedule.pdf"
    sample = PdfReader(Path(__file__).with_name("sample.pdf")).pages[0]
    text = sample.extract_text(extraction_mode="layout")
    assert text.strip()

    hass = MagicMock()
    hass.config.path.side_effect = lambda *parts: str(tmp_path.joinpath(*parts))
    (tmp_path / ".storage").mkdir()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    store = AsyncMock()
    store.async_load.return_value = {
        "page_count": 3,
        "pages": {"-1": "invalid", "0": "first", "1": "second", "2": "third"},
    }

    with patch("custom_components.pdf_scrape.pdf.get_store", return_value=store):
        reader = PDFScrapeFile(hass, "test_entry", source)

    writer = PdfWriter()
    if include_blank_page:
        writer.add_blank_page(width=612, height=792)
    writer.add_page(sample)
    writer.write(source)
    await reader.pdf_scrape()
    assert reader.pdf.pages == ({0: "", 1: text} if include_blank_page else {0: text})
    assert store.async_save.call_args.args[0]["pages"] == reader.pdf.pages

    writer = PdfWriter()
    writer.add_page(sample)
    writer.write(source)
    await reader.update()
    assert reader.pdf.pages == {0: text}
    assert not await reader.update()


@pytest.mark.asyncio
async def test_migrate_list_cache_uses_zero_based_keys() -> None:
    """Migrated pages use the same indices as newly extracted pages."""
    store = _PDFStore(MagicMock(), "test_entry")
    migrated = await store._async_migrate_func(1, 0, {"pages": ["first", "second"]})
    assert migrated["pages"] == {0: "first", 1: "second"}
    assert migrated["page_count"] == 2
