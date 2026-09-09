"""Unit tests for the error classifier used by the 14-day validation harness."""

from __future__ import annotations

import socket

import pytest
import requests

from app.services.validation.error_classifier import (
    AUTH_401,
    AUTH_403,
    CONNECTION,
    DNS,
    MALFORMED_4XX,
    RATE_LIMIT,
    SERVER_5XX,
    TIMEOUT,
    TLS,
    UNKNOWN,
    classify_error,
    classify_status_string,
)


class TestClassifyByException:
    def test_ssl_error_classifies_as_tls(self):
        assert classify_error(exc=requests.exceptions.SSLError("cert bad")) == TLS

    @pytest.mark.parametrize("exc", [
        requests.exceptions.ConnectTimeout("timed out"),
        requests.exceptions.ReadTimeout("read timeout"),
        requests.exceptions.Timeout("generic timeout"),
    ])
    def test_timeout_variants_classify_as_timeout(self, exc):
        assert classify_error(exc=exc) == TIMEOUT

    def test_connection_error_classifies_as_connection(self):
        assert classify_error(exc=requests.exceptions.ConnectionError("refused")) == CONNECTION

    def test_dns_failure_inside_connection_error_classifies_as_dns(self):
        exc = requests.exceptions.ConnectionError("NameResolutionError: Failed to resolve 'x.example.invalid'")
        assert classify_error(exc=exc) == DNS

    def test_gaierror_classifies_as_dns(self):
        assert classify_error(exc=socket.gaierror("Name or service not known")) == DNS

    def test_connection_refused_classifies_as_connection(self):
        assert classify_error(exc=ConnectionRefusedError("no")) == CONNECTION


class TestClassifyByStatus:
    @pytest.mark.parametrize("code, expected", [
        (401, AUTH_401),
        (403, AUTH_403),
        (429, RATE_LIMIT),
        (400, MALFORMED_4XX),
        (404, MALFORMED_4XX),
        (422, MALFORMED_4XX),
        (500, SERVER_5XX),
        (503, SERVER_5XX),
    ])
    def test_status_codes(self, code, expected):
        assert classify_error(status_code=code) == expected

    def test_success_code_returns_unknown(self):
        assert classify_error(status_code=200) == UNKNOWN

    def test_no_signal_returns_unknown(self):
        assert classify_error() == UNKNOWN


class TestExceptionTakesPrecedenceOverStatus:
    def test_timeout_exception_wins_over_500(self):
        assert classify_error(exc=requests.exceptions.ReadTimeout("t"), status_code=500) == TIMEOUT


class TestClassifyStatusString:
    @pytest.mark.parametrize("s, expected", [
        ("Read timed out", TIMEOUT),
        ("HTTPSConnectionPool timeout", TIMEOUT),
        ("SSL certificate expired", TLS),
        ("Name resolution failure", DNS),
        ("Connection reset by peer", CONNECTION),
        ("Unauthorized 401", AUTH_401),
        ("Forbidden 403", AUTH_403),
        ("circuit_open", "circuit_open"),
    ])
    def test_keyword_matches(self, s, expected):
        assert classify_status_string(s) == expected

    def test_empty_string_returns_unknown(self):
        assert classify_status_string("") == UNKNOWN

    def test_unrecognised_text_returns_unknown(self):
        assert classify_status_string("some random gibberish") == UNKNOWN
