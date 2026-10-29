# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Capture real EC2 instance-type facts for the gpu-pools Terraform tests (read-only).

Calls ``aws ec2 describe-instance-types`` (a read-only API) once per type, trying the regions in
``REGIONS`` until one offers the type, and writes ``instance-types.json`` next to this script in the
shape of the attributes of the ``aws_ec2_instance_type`` data source (the shape the ``gpu-pools``
module takes as input). The committed JSON is the source of truth of the tests; re-run this script
only to refresh it, never edit the JSON by hand.

Run: ``python3 capture_instance_types.py`` (needs the AWS CLI and credentials with
``ec2:DescribeInstanceTypes``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TYPES = [
    "p5.48xlarge",
    "p5en.48xlarge",
    "p6-b200.48xlarge",
    "p6-b300.48xlarge",
    "p4d.24xlarge",
    "p4de.24xlarge",
    "g5.xlarge",
    "g5.12xlarge",
    "g6.48xlarge",
    "g6e.xlarge",
    "g6e.2xlarge",
    "g6e.12xlarge",
    "g6e.48xlarge",
    "g7e.4xlarge",
    "g7e.48xlarge",
    "g4ad.4xlarge",
    "g5g.xlarge",
    "c7i.large",
]
REGIONS = ["us-east-1", "us-east-2", "us-west-2"]
OUT = Path(__file__).resolve().parent / "instance-types.json"


def describe(instance_type: str) -> dict | None:
    """Return the first describe-instance-types record for ``instance_type`` or None."""
    for region in REGIONS:
        proc = subprocess.run(
            ["aws", "ec2", "describe-instance-types", "--region", region,
             "--instance-types", instance_type, "--output", "json"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)["InstanceTypes"][0]
    return None


def to_data_source(rec: dict) -> dict:
    """Map a describe-instance-types record to ``aws_ec2_instance_type`` attribute names."""
    net = rec["NetworkInfo"]
    gpus = [
        {
            "name": g["Name"],
            "count": g["Count"],
            "memory_size": g["MemoryInfo"]["SizeInMiB"],
            "manufacturer": g["Manufacturer"],
        }
        for g in rec.get("GpuInfo", {}).get("Gpus", [])
    ]
    return {
        "maximum_network_cards": net["MaximumNetworkCards"],
        "efa_supported": net["EfaSupported"],
        # null (not 0) when EFA is unsupported: that is what the data source returns (no EfaInfo in the API).
        "efa_maximum_interfaces": net.get("EfaInfo", {}).get("MaximumEfaInterfaces"),
        "default_vcpus": rec["VCpuInfo"]["DefaultVCpus"],
        "memory_size": rec["MemoryInfo"]["SizeInMiB"],
        "total_instance_storage": rec.get("InstanceStorageInfo", {}).get("TotalSizeInGB", 0),
        "supported_architectures": rec["ProcessorInfo"]["SupportedArchitectures"],
        "gpus": gpus,
    }


def main() -> int:
    """Write instance-types.json; report the types no region offers."""
    out: dict[str, dict] = {}
    missing = []
    for t in TYPES:
        rec = describe(t)
        if rec is None:
            missing.append(t)
            continue
        out[t] = to_data_source(rec)
    OUT.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(out)} types to {OUT.name}; not offered in {REGIONS}: {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
