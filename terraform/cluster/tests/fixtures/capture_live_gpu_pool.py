# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Capture the live ``gpu-b200-cb`` NodeClass/NodePool spec for the golden-equivalence test (read-only).

Runs ``kubectl get nodeclass,nodepool <name> -o json`` (two read-only GETs) against the context in
``--context`` and writes ``live-gpu-b200-cb.json`` next to this script: only ``spec``, with the
account-specific identifiers and the zone replaced by the placeholders the golden test passes as
inputs, and with exactly the CRD-defaulted fields in ``EXCLUDED_CRD_DEFAULTS`` removed (server-side defaults
the Terraform render does not set). The exclusion list is fixed here and echoed into the JSON; it
must never be widened to make a failing test pass.

Run: ``python3 capture_live_gpu_pool.py --context <kube-context> [--name gpu-b200-cb]``
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parent / "live-gpu-b200-cb.json"
# advancedNetworking.* and top-level NodeClass spec fields the EKS Auto Mode CRD defaults server-side.
EXCLUDED_CRD_DEFAULTS = {
    "nodeclass.spec.advancedNetworking": ["enableV4Egress", "ipv4PrefixSize"],
    "nodeclass.spec": ["networkPolicy", "networkPolicyEventLogs", "snatPolicy"],
}
PLACEHOLDERS = {
    "subnet": "subnet-PLACEHOLDER",
    "security_group": "sg-PLACEHOLDER",
    "role": "node-role-PLACEHOLDER",
    "reservation": "cr-00000000000000000",
    "project": "dynamo-sample",
    "zone": "us-east-2c",
}
ZONE_KEY = "topology.kubernetes.io/zone"


def kget(context: str, kind: str, name: str) -> dict:
    """Read one object with a read-only ``kubectl get``."""
    out = subprocess.run(
        ["kubectl", "--context", context, "get", kind, name, "-o", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def sanitise_nodeclass(spec: dict) -> dict:
    """Drop the excluded defaults and replace account identifiers by placeholders."""
    spec = json.loads(json.dumps(spec))
    for key in EXCLUDED_CRD_DEFAULTS["nodeclass.spec"]:
        spec.pop(key, None)
    for key in EXCLUDED_CRD_DEFAULTS["nodeclass.spec.advancedNetworking"]:
        spec.get("advancedNetworking", {}).pop(key, None)
    spec["subnetSelectorTerms"] = [{"id": PLACEHOLDERS["subnet"]} for _ in spec["subnetSelectorTerms"]]
    spec["securityGroupSelectorTerms"] = [{"id": PLACEHOLDERS["security_group"]}]
    spec["role"] = PLACEHOLDERS["role"]
    spec["capacityReservationSelectorTerms"] = [
        {"id": PLACEHOLDERS["reservation"]} for _ in spec["capacityReservationSelectorTerms"]
    ]
    spec["tags"]["Project"] = PLACEHOLDERS["project"]
    return spec


def sanitise_nodepool(spec: dict) -> dict:
    """Replace the zone of the captured cluster by the placeholder zone (the golden test maps its AZ ID to it)."""
    spec = json.loads(json.dumps(spec))
    for req in spec["template"]["spec"]["requirements"]:
        if req["key"] == ZONE_KEY:
            req["values"] = [PLACEHOLDERS["zone"] for _ in req["values"]]
    return spec


def main() -> int:
    """Write the sanitised fixture."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True)
    ap.add_argument("--name", default="gpu-b200-cb")
    args = ap.parse_args()
    nodeclass = kget(args.context, "nodeclass", args.name)
    nodepool = kget(args.context, "nodepool", args.name)
    doc = {
        "source": "kubectl get nodeclass,nodepool (read-only), sanitised",
        "excluded_crd_defaults": EXCLUDED_CRD_DEFAULTS,
        "placeholders": PLACEHOLDERS,
        "nodeclass_spec": sanitise_nodeclass(nodeclass["spec"]),
        "nodepool_spec": sanitise_nodepool(nodepool["spec"]),
    }
    OUT.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
