"""Environment chip beside the wordmark on MCP browser pages."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from daimon.adapters.mcp.web_shell import configure_environment_label, render_page


@pytest.fixture(autouse=True)
def _reset_label() -> Iterator[None]:
    yield
    configure_environment_label(name="", label="")


def _header(name: str, label: str) -> str:
    configure_environment_label(name=name, label=label)
    body = render_page(title="t", body_html="").body.decode()
    return body.split("</header>")[0]


def test_production_shows_no_chip() -> None:
    assert "web-env" not in _header("", "")


@pytest.mark.parametrize(
    ("name", "label", "text"),
    [
        ("", "Staging", "Staging"),
        ("lane-preview", "Preview", "Preview"),
        ("lane-preview", "", "Lane preview"),
        ("qa_two", "", "Qa two"),
    ],
)
def test_chip_text_prefers_label_then_sentence_cased_name(name: str, label: str, text: str) -> None:
    header = _header(name, label)
    assert f'<span class="web-env" aria-label="Environment: {text}">{text}</span></div>' in header


def test_chip_text_is_escaped() -> None:
    header = _header("", "<b>x</b>")
    assert "<b>" not in header
    assert "Environment: &lt;b&gt;x&lt;/b&gt;" in header
