# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Scheme guard of the urllib HTTP adapter (Bandit B310: urlopen with a permitted-scheme audit).

Contract: ``UrllibHttpClient.request`` opens http and https URLs only. A ``file:``, ``ftp:`` or
host-less URL is refused with ValueError before anything is opened, and a redirect to a non-http(s)
URL is not followed: urllib's default redirect handler follows ``http -> ftp`` (only ``file:`` is
refused), so the adapter must refuse it itself and return the 3xx status (HTTP statuses are returned,
never raised). The tests run the real adapter against a local server (127.0.0.1, no network).
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest


class _Redirects(BaseHTTPRequestHandler):
    """``/to?<url>`` answers 302 to ``<url>``; ``/ok`` answers 200 ``ok``."""

    def do_GET(self) -> None:
        if self.path.startswith("/to?"):
            self.send_response(302)
            self.send_header("Location", self.path[len("/to?") :])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence the request log."""


@pytest.fixture
def base_url() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Redirects)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://127.0.0.1/x",
        "http:///no-host",
        "data:text/plain,x",
        "/relative",
    ],
)
def test_non_http_urls_are_refused_before_anything_is_opened(url: str) -> None:
    from demo_bench.infrastructure.http import UrllibHttpClient

    with (
        mock.patch.object(urllib.request.OpenerDirector, "open") as opened,
        pytest.raises(ValueError, match="non-http"),
    ):
        UrllibHttpClient().request("GET", url)
    opened.assert_not_called()


def test_a_redirect_to_ftp_is_not_followed(base_url: str) -> None:
    from demo_bench.infrastructure.http import UrllibHttpClient

    ftp = mock.Mock(side_effect=urllib.error.URLError("the adapter opened an ftp URL"))
    with mock.patch.object(urllib.request.FTPHandler, "ftp_open", ftp):
        resp = UrllibHttpClient(default_timeout_s=5).request(
            "GET", f"{base_url}/to?ftp://127.0.0.1:9/x"
        )
    ftp.assert_not_called()
    assert resp.status_code == 302


def test_a_redirect_to_file_is_not_followed(base_url: str) -> None:
    from demo_bench.infrastructure.http import UrllibHttpClient

    resp = UrllibHttpClient(default_timeout_s=5).request("GET", f"{base_url}/to?file:///etc/passwd")
    assert resp.status_code == 302
    assert "root:" not in resp.text


def test_a_redirect_to_http_is_still_followed(base_url: str) -> None:
    from demo_bench.infrastructure.http import UrllibHttpClient

    resp = UrllibHttpClient(default_timeout_s=5).request("GET", f"{base_url}/to?{base_url}/ok")
    assert (resp.status_code, resp.text) == (200, "ok")
