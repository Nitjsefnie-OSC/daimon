import io
import uuid

import pytest
from daimon.adapters.discord import tables
from daimon.adapters.discord.tables import render_discord_tables, tables_enabled
from PIL import Image


async def test_wide_table_is_a_readable_bounded_png():
    headers = [f"Column {i} wide heading" for i in range(12)]
    table = "| " + " | ".join(headers) + " |\n"
    table += "| " + " | ".join(["---"] * 12) + " |\n"
    table += "| " + " | ".join(["123.45"] * 12) + " |\n"
    text, files = await render_discord_tables("Before\n" + table + "After", enabled=True)
    assert "Before" in text and "After" in text
    assert "| ---" not in text
    assert len(files) == 1
    image = Image.open(io.BytesIO(files[0].fp.read()))
    assert image.format == "PNG"
    assert image.width == 2400
    assert image.height >= 98
    assert image.getpixel((0, 0)) == (12, 31, 64)
    assert "table-1.png" in text


async def test_non_table_text_is_unchanged():
    assert await render_discord_tables("ordinary **answer**") == ("ordinary **answer**", [])


async def test_disabled_rendering_keeps_table_text():
    table = "| A | B |\n| --- | --- |\n| a | b |"
    assert await render_discord_tables(table) == (table, [])


async def test_unsupported_glyphs_preserve_distinct_original_values():
    outputs = []
    for value in ("中文", "日本"):
        table = f"| Value |\n| --- |\n| {value} |\n"
        text, files = await render_discord_tables(table, enabled=True)
        assert text == table
        assert value in text
        assert files == []
        outputs.append(text)
    assert outputs[0] != outputs[1]


async def test_unsupported_header_preserves_table_but_supported_neighbor_renders():
    raw = "| 中文 |\n| --- |\n| 123 |\n"
    supported = "| Value |\n| --- |\n| 456 |\n"
    text, files = await render_discord_tables(raw + "\n" + supported, enabled=True)
    assert raw in text
    assert len(files) == 1


def test_tables_render_by_default_and_explicit_false_opts_out():
    tenant = uuid.uuid4()
    assert tables_enabled({}, tenant) is True
    assert tables_enabled({tenant: True}, tenant) is True
    assert tables_enabled({tenant: False}, tenant) is False


@pytest.mark.parametrize("cell", ["short", "x" * 2000])
async def test_attachment_description_is_raw_table_truncated_to_discord_limit(cell):
    table = f"| Value |\n| --- |\n| {cell} |\n"
    _, files = await render_discord_tables(table, enabled=True)
    assert files[0].description == table[:1024]


async def test_render_failure_keeps_raw_table_text(monkeypatch):
    def fail(_table):
        raise RuntimeError("render failed")

    monkeypatch.setattr(tables, "table_png", fail)
    table = "| A | B |\n| --- | --- |\n| a | b |\n"
    assert await render_discord_tables(table, enabled=True) == (table, [])
