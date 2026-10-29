# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Standard-library HTTP adapter for the :class:`~demo_bench.domain.ports.HttpClient` port."""

from __future__ import annotations

import json as jsonlib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import IO, Any

from demo_bench.domain.ports import HttpTransportError

_ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_TIMEOUT_S = 30.0


class _HttpOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects to http(s) URLs only (urllib's default handler also follows ``ftp:``)."""

    def redirect_request(  # noqa: PLR0917 - urllib's HTTPRedirectHandler signature
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Refuse a redirect to another scheme; delegate the rest to urllib.

        Args:
            req: The request that was redirected.
            fp: The redirect response body.
            code: The 3xx status.
            msg: The status reason.
            headers: The redirect response headers.
            newurl: The absolute redirect target.

        Returns:
            The follow-up request, or None when urllib does not follow this redirect.

        Raises:
            urllib.error.HTTPError: The target is not an http(s) URL (the 3xx is then returned as
                the response, not followed).
        """
        if urllib.parse.urlsplit(newurl).scheme not in _ALLOWED_SCHEMES:
            raise urllib.error.HTTPError(
                newurl, code, f"{msg} - redirect to a non-http(s) URL refused", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class UrllibResponse:
    """HTTP response returned by :class:`UrllibHttpClient`.

    Attributes:
        status_code: HTTP status code.
        text: Body decoded as UTF-8 (replacement on invalid bytes).
    """

    status_code: int
    text: str

    def json(self) -> Any:
        """Decode the body as JSON.

        Returns:
            The decoded document.
        """
        return jsonlib.loads(self.text)


class UrllibHttpClient:
    """Synchronous HTTP client on :mod:`urllib.request` (no third-party dependency)."""

    def __init__(self, default_timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        """Create the client.

        Args:
            default_timeout_s: Timeout used when a request does not set one.
        """
        self._default_timeout_s = default_timeout_s
        # urllib's default opener with the redirect handler above: with the scheme check of
        # ``request`` no URL other than http(s) is ever opened, redirects included.
        self._opener = urllib.request.build_opener(_HttpOnlyRedirectHandler)

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> UrllibResponse:
        """Send one request; HTTP error statuses are returned, not raised.

        Args:
            method: HTTP method.
            url: Absolute ``http``/``https`` URL.
            json: Optional JSON body.
            params: Optional query-string parameters.
            headers: Optional headers.
            timeout: Timeout in seconds.

        Returns:
            The response.

        Raises:
            ValueError: If the URL scheme is not http(s) or the URL has no host.
            HttpTransportError: If no response was received.
        """
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.netloc:
            raise ValueError(f"refusing non-http(s) URL {url!r}")
        if params:
            sep = "&" if parsed.query else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(dict(params))}"
        req_headers = {"Accept": "application/json", **dict(headers or {})}
        data: bytes | None = None
        if json is not None:
            data = jsonlib.dumps(json).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(  # noqa: S310 - scheme validated above
            url, data=data, headers=req_headers, method=method.upper()
        )
        effective_timeout = self._default_timeout_s if timeout is None else timeout
        try:
            with self._opener.open(request, timeout=effective_timeout) as resp:
                return UrllibResponse(resp.status, resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if exc.fp is not None else ""
            return UrllibResponse(exc.code, body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HttpTransportError(f"{method} {url}: {exc}") from exc
