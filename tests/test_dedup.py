"""A-10. URL 정규화 (ContentItem 연결 전용)."""

from __future__ import annotations

import pytest

from ria.core.dedup import is_tracking_param, normalize_url, same_content, url_key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.COM/Article", "https://example.com/Article"),
        ("https://example.com/article/", "https://example.com/article"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com", "https://example.com/"),
        ("  https://example.com/a  ", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/a#section-2", "https://example.com/a"),
    ],
)
def test_basic_normalization(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_path_case_is_preserved() -> None:
    """호스트는 대소문자를 구분하지 않지만 경로는 구분한다."""
    assert normalize_url("https://example.com/AbC") != normalize_url("https://example.com/abc")


def test_tracking_parameters_are_removed() -> None:
    normalized = normalize_url(
        "https://example.com/a?utm_source=hn&utm_medium=social&fbclid=xyz&id=42"
    )

    assert normalized == "https://example.com/a?id=42"


def test_query_order_does_not_matter() -> None:
    assert normalize_url("https://example.com/a?b=2&a=1") == normalize_url(
        "https://example.com/a?a=1&b=2"
    )


def test_meaningful_query_is_kept() -> None:
    assert normalize_url("https://example.com/watch?v=abc") == "https://example.com/watch?v=abc"


def test_blank_query_value_is_kept() -> None:
    assert normalize_url("https://example.com/a?flag=") == "https://example.com/a?flag="


@pytest.mark.parametrize(
    "name",
    ["utm_source", "UTM_Campaign", "fbclid", "gclid", "igshid", "_hsenc", "mtm_source"],
)
def test_tracking_param_detection(name: str) -> None:
    assert is_tracking_param(name) is True


@pytest.mark.parametrize("name", ["id", "v", "page", "q", "reference"])
def test_non_tracking_param_detection(name: str) -> None:
    assert is_tracking_param(name) is False


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "mailto:someone@example.com",
        "javascript:void(0)",
        "/relative/path",
        "not a url",
    ],
)
def test_unnormalizable_input_returns_none(raw: str | None) -> None:
    assert normalize_url(raw) is None


def test_www_is_not_stripped() -> None:
    """다른 문서를 같은 것으로 묶지 않는다."""
    assert normalize_url("https://www.example.com/a") != normalize_url("https://example.com/a")


def test_scheme_is_not_upgraded() -> None:
    assert normalize_url("http://example.com/a") != normalize_url("https://example.com/a")


def test_url_key_matches_normalize_url() -> None:
    raw = "https://Example.com/a/?utm_source=x"

    assert url_key(raw) == normalize_url(raw)


def test_same_content_links_platform_variants() -> None:
    """HN 과 Reddit 이 같은 기사에 서로 다른 트래킹 꼬리를 붙여도 같은 ContentItem 이다."""
    assert (
        same_content(
            "https://example.com/article?utm_source=hn",
            "https://Example.com/article/?fbclid=abc",
        )
        is True
    )


def test_same_content_is_false_when_unnormalizable() -> None:
    assert same_content(None, "https://example.com/a") is False
    assert same_content("not a url", "not a url") is False
