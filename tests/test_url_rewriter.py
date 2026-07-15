"""Tests for Docker URL rewriting."""

from app.frost.url_rewriter import rewrite_url


def test_rewrites_matching_url():
    result = rewrite_url(
        "http://frost.public.com/v1.1/Things?$skip=100",
        "http://frost.public.com/v1.1",
        "http://frost:8080/FROST-Server/v1.1",
    )
    assert result == "http://frost:8080/FROST-Server/v1.1/Things?$skip=100"


def test_no_match_returns_unchanged():
    url = "http://other.server/v1.1/Things"
    assert rewrite_url(url, "http://frost.public.com/v1.1", "http://internal") == url


def test_trailing_slashes_handled():
    result = rewrite_url(
        "http://frost.public.com/v1.1/Things",
        "http://frost.public.com/v1.1/",
        "http://internal/v1.1/",
    )
    assert result == "http://internal/v1.1/Things"


def test_empty_bases_return_unchanged():
    url = "http://frost/v1.1/Things"
    assert rewrite_url(url, "", "http://internal") == url
    assert rewrite_url(url, "http://frost/v1.1", "") == url
