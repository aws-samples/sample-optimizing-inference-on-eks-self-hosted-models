# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Environment for the script contract tests.

Since the kube-context guard (``test_95_kube_context.py``) the scripts refuse to run off-cluster
unless ``KUBE_CONTEXT`` is set, and (Track Q is the only profile) the context must be the ``CLUSTER_NAME`` of the platform config,
so every script test runs with that cluster unless it overrides the environment itself.  ``KUBERNETES_SERVICE_HOST`` is cleared so a CI pod cannot mask the guard.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _kube_context_for_script_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBE_CONTEXT", "dynamo-sample-use2")  # CLUSTER_NAME of the placeholder platform.env
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
