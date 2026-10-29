# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""demo_bench: benchmark tooling for the Dynamo on EKS demo (spec 12.1)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("demo-bench")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
