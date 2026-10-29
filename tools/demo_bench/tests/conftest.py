# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared fixtures and fakes for the demo_bench test suite.

Nothing here imports ``demo_bench``: tests import the package inside each test so a missing
implementation shows up as a test failure (RED), never as a collection error or a skip.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the directory with static test fixtures."""
    return FIXTURES


@dataclass
class FakeResponse:
    """Minimal HTTP response satisfying ``demo_bench.domain.ports.HttpResponse``."""

    status_code: int = 200
    body: Any = field(default_factory=dict)
    text: str = ""

    def json(self) -> Any:
        """Return the decoded JSON body."""
        return self.body

    def __post_init__(self) -> None:
        if not self.text:
            self.text = json.dumps(self.body)


@dataclass
class RecordedRequest:
    """One request captured by :class:`FakeHttpClient`."""

    method: str
    url: str
    json: Any
    params: Mapping[str, str] | None
    headers: Mapping[str, str] | None


class FakeHttpClient:
    """Programmable fake for ``demo_bench.domain.ports.HttpClient`` (no network).

    ``handler`` receives each :class:`RecordedRequest` and returns a :class:`FakeResponse`.
    """

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.requests: list[RecordedRequest] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        """Record the request and delegate to the handler."""
        rec = RecordedRequest(method, url, json, params, headers)
        self.requests.append(rec)
        response: FakeResponse = self.handler(rec)
        return response


@pytest.fixture
def fake_http_client_cls() -> type[FakeHttpClient]:
    """Expose the fake client class."""
    return FakeHttpClient


@pytest.fixture
def fake_response_cls() -> type[FakeResponse]:
    """Expose the fake response class."""
    return FakeResponse
