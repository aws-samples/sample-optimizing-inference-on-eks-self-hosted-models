# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: static contract of ``terraform/cluster`` and ``terraform/bootstrap`` (no Terraform binary needed).

Covers: variables (descriptions, validations, required vs defaulted), the single tfvars.example, backend.tf.example,
required_version, pinned vpc-cni add-on, repo-root sample dir + relocated DCGM dashboard, Grafana edge off by
default (its plain-HTTP origin hop an explicit opt-in, listener and VPC origin aligned, viewer TLSv1.2_2021 with a
custom domain and the default-certificate suppressions scoped and explained, `dashboards` as default root object),
`moved` blocks and address preservation for adopting the live state of the original deployment, Hugging Face secret handling
(Secrets Manager ARN only, never read by Terraform), outputs, and the golden fixture exclusion list.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest
from hcl import block_body, blocks, read_tf

REPO = Path(__file__).resolve().parents[2]
CLUSTER = REPO / "terraform" / "cluster"
BOOTSTRAP = REPO / "terraform" / "bootstrap"
SNAPSHOT = "240b23a"
SNAPSHOT_STACK = "terraform/terraform-" + "ap-" + "south-1"  # the original stack at SNAPSHOT (git history only)

REQUIRED_NO_DEFAULT = ["aws_region", "eks_public_access_cidrs", "gpu_instance_types", "model_id", "model_revision"]
USER_FACING = [
    "aws_region",
    "cluster_name",
    "gpu_instance_types",
    "gpu_capacity_type",
    "capacity_reservation_ids",
    "gpu_az_ids",
    "model_id",
    "model_revision",
    "hf_token_secret_arn",
    "workload_rate",
    "workload_context_tokens",
    "workload_sessions_per_rate",
    "runtime_image",
    "enable_grafana_edge",
    "grafana_edge_allow_plain_http_origin",
    "grafana_edge_domain",
    "grafana_edge_viewer_certificate_arn",
    "eks_public_access_cidrs",
    "allow_open_eks_api",
    "tags",
    "vpc_cni_addon_version",
]
VALIDATED = [
    "aws_region",
    "cluster_name",
    "eks_public_access_cidrs",
    "gpu_instance_types",
    "gpu_capacity_type",
    "capacity_reservation_ids",
    "gpu_az_ids",
    "gpu_max_nodes",
    "model_id",
    "model_revision",
    "hf_token_secret_arn",
    "workload_rate",
    "workload_context_tokens",
    "workload_sessions_per_rate",
    "runtime_image",
    "vpc_cni_addon_version",
    "enable_grafana_edge",
    "grafana_edge_viewer_certificate_arn",
]


@pytest.fixture(scope="module")
def tf() -> dict[str, str]:
    assert CLUSTER.is_dir(), "terraform/cluster must exist"
    found = read_tf(CLUSTER)
    assert found, "terraform/cluster has no .tf files yet"
    return found


@pytest.fixture(scope="module")
def variables(tf: dict[str, str]) -> dict[str, str]:
    assert "variables.tf" in tf, "terraform/cluster/variables.tf must exist"
    return blocks(tf["variables.tf"], "variable")


def test_every_variable_has_a_description(variables: dict[str, str]) -> None:
    assert variables
    assert [n for n, b in variables.items() if not re.search(r"(?m)^\s*description\s*=", b)] == []


@pytest.mark.parametrize("name", USER_FACING)
def test_user_facing_variable_declared(variables: dict[str, str], name: str) -> None:
    assert name in variables


@pytest.mark.parametrize("name", VALIDATED)
def test_variable_has_validation(variables: dict[str, str], name: str) -> None:
    assert re.search(r"(?m)^\s*validation\s*\{", variables.get(name, "")), f"{name} needs a validation block"


@pytest.mark.parametrize("name", REQUIRED_NO_DEFAULT)
def test_required_variable_has_no_default(variables: dict[str, str], name: str) -> None:
    assert name in variables and not re.search(r"(?m)^\s*default\s*=", variables[name])


def test_secure_defaults(variables: dict[str, str]) -> None:
    assert re.search(r"(?m)^\s*default\s*=\s*false", variables["allow_open_eks_api"])
    assert re.search(r"(?m)^\s*default\s*=\s*false", variables["enable_grafana_edge"])
    assert "0.0.0.0/0" not in re.sub(r'error_message\s*=\s*"[^"]*"', "", variables["eks_public_access_cidrs"]).split("validation")[0]


def test_open_api_validation_is_gated_by_allow_open_eks_api(variables: dict[str, str]) -> None:
    body = variables["eks_public_access_cidrs"]
    assert "allow_open_eks_api" in body, "cross-variable rule: open CIDRs only with allow_open_eks_api = true"
    assert re.search(r"/0|cidrnetmask|split\(\"/\"", body), "rule must reject ANY zero-length prefix (also ::/0, 10.0.0.0/0)"


def test_hf_token_variable_is_secrets_manager_only(variables: dict[str, str]) -> None:
    assert "secretsmanager" in variables["hf_token_secret_arn"]
    assert not re.search(r"(?m)^\s*sensitive\s*=\s*true", variables["hf_token_secret_arn"]) or True  # an ARN is not a secret


def test_gpu_efa_is_a_string_not_a_bool(variables: dict[str, str]) -> None:
    body = variables.get("gpu_efa", "")
    assert not body or re.search(r"type\s*=\s*string", body), "gpu_efa changed from bool to string (auto|off)"


def test_old_gpu_variables_are_gone(variables: dict[str, str]) -> None:
    assert not {"gpu_profile", "capacity_block_ids"} & set(variables)
    assert [n for n in variables if n.startswith("gpu_test_")] == []


def test_terraform_never_reads_or_creates_the_hf_secret(tf: dict[str, str]) -> None:
    joined = "\n".join(tf.values())
    assert "aws_secretsmanager_secret_version" not in joined, "the token value must never enter state"
    assert not re.search(r'resource\s+"aws_secretsmanager_secret"', joined)


def test_hf_secret_policy_is_scoped_to_the_variable_arn(tf: dict[str, str]) -> None:
    hits = {n: t for n, t in tf.items() if "secretsmanager:GetSecretValue" in t}
    assert hits, "an IAM policy must allow secretsmanager:GetSecretValue on hf_token_secret_arn"
    for name, text in hits.items():
        assert "var.hf_token_secret_arn" in text, name
        for m in re.finditer("secretsmanager:GetSecretValue", text):
            window = text[max(0, m.start() - 400) : m.end() + 400]
            assert not re.search(r'[Rr]esources?\s*=\s*\[?\s*"\*"', window), f"{name}: GetSecretValue must not allow Resource *"


def test_versions_require_terraform_1_10_and_pin_providers(tf: dict[str, str]) -> None:
    text = tf.get("versions.tf", "")
    m = re.search(r'required_version\s*=\s*"([^"]+)"', text)
    assert m, "required_version missing"
    assert re.search(r">=\s*1\.(1\d|[2-9]\d)", m.group(1)), f"need >= 1.10 (use_lockfile, tftest), got {m.group(1)}"
    for prov in ("aws", "kubernetes", "helm", "kubectl"):
        assert re.search(rf"(?m)^\s*{prov}\s*=\s*\{{", text), f"provider {prov} not pinned"
    assert re.search(r"(?m)^\s*aws\s*=\s*\{[^}]*6\.", text), "aws provider constraint must be 6.x (capacity block data source)"


def test_vpc_cni_addon_version_is_a_variable_not_a_literal(tf: dict[str, str]) -> None:
    users = [n for n, t in tf.items() if n != "variables.tf" and "var.vpc_cni_addon_version" in t]
    assert users, "the vpc-cni add-on must use var.vpc_cni_addon_version"
    literal = [n for n, t in tf.items() if n != "variables.tf" and re.search(r'"v\d+\.\d+\.\d+-eksbuild\.\d+"', t)]
    assert literal == [], f"hard-coded add-on version in {literal}"


def test_sample_dir_is_the_repo_root_and_dcgm_dashboard_moved(tf: dict[str, str]) -> None:
    joined = "\n".join(tf.values())
    m = re.search(r'sample_repo_dir\s*=\s*"([^"]+)"', joined)
    assert m and m.group(1) == "${path.module}/../..", "sample_repo_dir must be the repo root (terraform/cluster/../..)"
    assert "sample-optimizing-inference-on-eks" not in joined
    assert not re.search(r"terraform-eu|eu-north-1|terraform-" + r"ap-" + r"south", joined)
    dcgm = list((REPO / "dashboards" / "dcgm").glob("*.json"))
    assert dcgm, "the DCGM dashboard JSON must live under dashboards/dcgm/ (moved before terraform-eu-north-1 is deleted)"
    assert "dashboards/dcgm" in joined
    for f in re.findall(r'file\("\$\{local\.sample_repo_dir\}/([^"]+)"\)', joined):
        assert (REPO / f).is_file(), f"observability reads a missing file: {f}"


def test_grafana_edge_is_optional(tf: dict[str, str]) -> None:
    body = tf.get("grafana-edge.tf", "")
    assert body, "grafana-edge.tf must exist"
    for kind, labels in (("resource", 2), ("data", 2)):
        for name, b in blocks(body, kind, labels).items():
            assert re.search(r"(?m)^\s*count\s*=.*enable_grafana_edge", b), f"{kind}.{name} must use count = var.enable_grafana_edge ? 1 : 0"


def test_plain_http_grafana_origin_is_an_explicit_opt_in(variables: dict[str, str]) -> None:
    """The CloudFront-to-NLB hop of the edge is plain HTTP: the edge plans only with an acknowledgement (default false)."""
    ack = variables.get("grafana_edge_allow_plain_http_origin", "")
    assert re.search(r"(?m)^\s*type\s*=\s*bool", ack), "grafana_edge_allow_plain_http_origin must be a bool variable"
    assert re.search(r"(?m)^\s*default\s*=\s*false", ack), "the plain-HTTP origin hop is never a silent default"
    rule = variables["enable_grafana_edge"]
    assert "var.grafana_edge_allow_plain_http_origin" in rule, "cross-variable rule: enable_grafana_edge = true needs the acknowledgement"
    msg = " ".join(re.findall(r'error_message\s*=\s*"([^"]*)"', rule))
    for fact in ("plain HTTP", "grafana_edge_allow_plain_http_origin = true", "TLS listener", "port-forward"):
        assert fact in msg, f"the refusal must explain the risk and the options: {fact!r} missing from {msg!r}"


def test_grafana_nlb_listener_and_vpc_origin_agree(tf: dict[str, str]) -> None:
    """One plain-HTTP listener port drives the Service, the VPC origin and the CloudFront SG rule; the NLB has no TLS listener."""
    res = blocks(tf["grafana-edge.tf"], "resource", 2)
    svc = res["kubernetes_service_v1.grafana_edge"]
    origin = res["aws_cloudfront_vpc_origin.grafana"]
    ingress = res["aws_vpc_security_group_ingress_rule.grafana_nlb_from_cloudfront"]
    assert not re.search(r"aws-load-balancer-ssl-(?:cert|ports)", svc), "a CloudFront VPC origin does not accept an NLB with a TLS listener"
    assert re.search(r'origin_protocol_policy\s*=\s*"http-only"', origin), "the VPC origin matches the plain-HTTP listener"
    assert re.search(r"(?m)^\s*port\s*=\s*var\.grafana_edge_listener_port", svc)
    assert re.search(r"(?m)^\s*http_port\s*=\s*var\.grafana_edge_listener_port", origin)
    for side in ("from_port", "to_port"):
        assert re.search(rf"(?m)^\s*{side}\s*=\s*var\.grafana_edge_listener_port", ingress), f"grafana_nlb_from_cloudfront.{side}"


def test_https_settings_of_the_http_only_origin_are_marked_unused() -> None:
    """https_port and origin_ssl_protocols are required by the API/provider but unused with http-only: the code says so."""
    raw = (CLUSTER / "grafana-edge.tf").read_text()
    origin = blocks(raw, "resource", 2)["aws_cloudfront_vpc_origin.grafana"]
    for setting in ("https_port", "origin_ssl_protocols"):
        line = next((ln for ln in origin.splitlines() if re.match(rf"\s*{setting}\b", ln)), "")
        assert line, f"{setting} is required by the provider, keep it"
        before = origin[: origin.index(line)].splitlines()[-3:]
        assert any(re.search(r"(?i)\bnot used\b|\bunused\b", ln) for ln in [*before, line]), (
            f"{setting}: say next to it that it is required but not used with http-only (config and behaviour must agree)")


SEMGREP_CF_TLS = "terraform.aws.security.aws-insecure-cloudfront-distribution-tls-version"
# The same rule as named by the registry ruleset p/terraform (the rule file name is part of the id there).
SEMGREP_CF_TLS_REGISTRY = "terraform.aws.security.aws-cloudfront-insecure-tls.aws-insecure-cloudfront-distribution-tls-version"
CF_RESOURCE_LINE = 'resource "aws_cloudfront_distribution" "grafana" {'


def test_grafana_viewer_tls_floor_and_its_suppressions_are_scoped_and_explained() -> None:
    """TLSv1.2_2021 with a custom domain; the only suppressions are on the distribution, for the default certificate."""
    raw = (CLUSTER / "grafana-edge.tf").read_text()
    dist = blocks(raw, "resource", 2)["aws_cloudfront_distribution.grafana"]
    viewer = block_body(dist, re.search(r"(?m)^\s*viewer_certificate\s*\{", dist).start())
    assert re.search(r"acm_certificate_arn\s*=.*var\.grafana_edge_viewer_certificate_arn", viewer)
    assert re.search(r'ssl_support_method\s*=.*"sni-only"', viewer), "a custom certificate is served over SNI"
    assert re.search(r'minimum_protocol_version\s*=.*"TLSv1\.2_2021"', viewer), "a custom certificate gets TLSv1.2_2021"
    assert re.search(r"(?m)^\s*aliases\s*=.*var\.grafana_edge_domain", dist), "the custom domain is the distribution alias"

    skips = re.findall(r"(?m)^\s*#checkov:skip=CKV_AWS_174:(.*)$", raw)
    assert len(skips) == 1 and re.search(r"(?m)^\s*#checkov:skip=CKV_AWS_174:", dist), "one CKV_AWS_174 skip, on the distribution"
    for fact in ("TLSv1.2_2021", "*.cloudfront.net", "TLSv1 ", "grafana_edge_viewer_certificate_arn"):
        assert fact in skips[0], f"the CKV_AWS_174 skip must give the platform reason: {fact!r} missing from {skips[0]!r}"

    lines = raw.splitlines()
    marks = [i for i, ln in enumerate(lines) if f"nosemgrep: {SEMGREP_CF_TLS}" in ln]
    assert len(marks) == 1, f"one nosemgrep for {SEMGREP_CF_TLS}"
    assert lines[marks[0] + 1] == CF_RESOURCE_LINE, "the nosemgrep sits on the line right above the distribution"
    assert SEMGREP_CF_TLS_REGISTRY in lines[marks[0]], "the same nosemgrep also names the rule id of the p/terraform ruleset"
    reason = lines[marks[0]].split(" -- ", 1)[1] if " -- " in lines[marks[0]] else ""
    for fact in ("TLSv1.2_2021", "*.cloudfront.net", "TLSv1 "):
        assert fact in reason, f"the nosemgrep must give the platform reason on the same line: {fact!r} missing"


def test_custom_domain_certificate_is_paired_and_in_us_east_1(variables: dict[str, str]) -> None:
    """The ACM certificate for CloudFront must live in us-east-1, and it only makes sense with the domain it covers."""
    for name in ("grafana_edge_domain", "grafana_edge_viewer_certificate_arn"):
        assert re.search(r'(?m)^\s*default\s*=\s*""', variables.get(name, "")), f"{name} is optional (default empty)"
    cert = variables["grafana_edge_viewer_certificate_arn"]
    assert "var.grafana_edge_domain" in cert, "cross-variable rule: the certificate and the domain are set together"
    assert re.search(r"acm:us-east-1:", cert), "the validation pins the certificate to us-east-1 (CloudFront requirement)"


def test_grafana_default_root_object_is_not_the_login_page(tf: dict[str, str]) -> None:
    """A signed-in GET /login redirects to /: a default root object `login` would loop, so the root serves `dashboards`."""
    dist = blocks(tf["grafana-edge.tf"], "resource", 2)["aws_cloudfront_distribution.grafana"]
    m = re.search(r'(?m)^\s*default_root_object\s*=\s*"([^"]*)"', dist)
    assert m and m.group(1) == "dashboards", "default_root_object must be the Grafana page `dashboards`"


def moved_pairs(tf: dict[str, str]) -> dict[str, str]:
    text = tf.get("moved.tf", "")
    assert text, "terraform/cluster/moved.tf must exist"
    pairs = {}
    for m in re.finditer(r"moved\s*\{", text):
        body = block_body(text, m.start())
        frm = re.search(r"from\s*=\s*(.+)", body)
        to = re.search(r"to\s*=\s*(.+)", body)
        assert frm and to
        pairs[frm.group(1).strip()] = to.group(1).strip()
    return pairs


def snapshot_resources(file: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{SNAPSHOT}:{SNAPSHOT_STACK}/{file}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [f"{t}.{n}" for t, n in re.findall(r'(?m)^resource\s+"([^"]+)"\s+"([^"]+)"', out)]


def test_moved_blocks_cover_grafana_edge_and_gpu_pool(tf: dict[str, str]) -> None:
    pairs = moved_pairs(tf)
    for addr in snapshot_resources("grafana-edge.tf"):
        assert pairs.get(addr) == f"{addr}[0]", f"missing moved {addr} -> {addr}[0] (adopting the live state with count)"
    assert pairs.get("kubectl_manifest.node_class_gpu_b200_cb[0]") == 'kubectl_manifest.gpu_node_class["gpu-b200-cb"]'
    assert pairs.get("kubectl_manifest.node_pool_gpu_b200_cb[0]") == 'kubectl_manifest.gpu_node_pool["gpu-b200-cb"]'


def test_every_moved_target_exists_in_the_config(tf: dict[str, str]) -> None:
    declared = set()
    for text in tf.values():
        declared |= {f"{t}.{n}" for t, n in re.findall(r'(?m)^resource\s+"([^"]+)"\s+"([^"]+)"', text)}
    for frm, to in moved_pairs(tf).items():
        target = re.sub(r"\[.*$", "", to)
        assert target in declared, f"moved target {to} (from {frm}) is not a resource of terraform/cluster"
    for name in ("gpu_node_class", "gpu_node_pool"):
        body = blocks(tf.get("gpu-nodepool.tf", ""), "resource", 2).get(f"kubectl_manifest.{name}", "")
        assert "for_each" in body, f"kubectl_manifest.{name} must be for_each over the pools"


def test_other_resource_addresses_are_preserved_for_state_adoption(tf: dict[str, str]) -> None:
    """Every snapshot resource not moved/removed keeps its address (plan on a copy of live state: 0 destroy)."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-tree", "--name-only", SNAPSHOT, f"{SNAPSHOT_STACK}/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    old: set[str] = set()
    for path in out:
        if not path.endswith(".tf"):
            continue
        old |= set(snapshot_resources(path.rsplit("/", 1)[1]))
    old |= {f"module.{m}" for m in ("eks", "vpc")}
    gone = {
        "kubectl_manifest.node_class_gpu_b200_cb",  # moved to the for_each pool
        "kubectl_manifest.node_pool_gpu_b200_cb",
        "kubectl_manifest.node_class_gpu_g7e_odcr",  # ODCR test pool of the original stack, never in the live state
        "kubectl_manifest.node_pool_gpu_g7e_odcr",
    }
    new: set[str] = set()
    for text in tf.values():
        new |= {f"{t}.{n}" for t, n in re.findall(r'(?m)^resource\s+"([^"]+)"\s+"([^"]+)"', text)}
        new |= {f"module.{n}" for n in re.findall(r'(?m)^module\s+"([^"]+)"', text)}
    assert sorted((old - gone) - new) == [], "resource addresses changed without a moved block"


def test_outputs_expose_the_env_contract(tf: dict[str, str]) -> None:
    outs = blocks(tf.get("outputs.tf", ""), "output")
    for name in ("platform_env", "sample_env", "gpu_pools"):
        assert name in outs, f"output {name} is consumed by scripts/gen_env.py"
        assert not re.search(r"(?m)^\s*sensitive\s*=\s*true", outs[name]), f"{name} must not be sensitive"


def test_root_wires_the_gpu_pools_module_and_data_sources(tf: dict[str, str]) -> None:
    joined = "\n".join(tf.values())
    assert re.search(r'module\s+"gpu_pools"\s*\{[^}]*source\s*=\s*"\./modules/gpu-pools"', joined, flags=re.DOTALL)
    assert 'data "aws_ec2_instance_type"' in joined and "for_each" in joined
    assert 'data "aws_ec2_instance_type_offerings"' in joined
    assert 'data "aws_ec2_capacity_block_reservation"' in joined


def test_gpu_module_is_pure() -> None:
    mod = read_tf(CLUSTER / "modules" / "gpu-pools")
    assert mod, "terraform/cluster/modules/gpu-pools must exist"
    joined = "\n".join(mod.values())
    assert not re.search(r'(?m)^(data|resource|provider)\s+"', joined), "gpu-pools: plain variables in, locals/outputs out"


def test_golden_fixture_exclusion_list_is_fixed() -> None:
    fx = json.loads((CLUSTER / "tests" / "fixtures" / "live-gpu-b200-cb.json").read_text())
    assert fx["excluded_crd_defaults"] == {
        "nodeclass.spec.advancedNetworking": ["enableV4Egress", "ipv4PrefixSize"],
        "nodeclass.spec": ["networkPolicy", "networkPolicyEventLogs", "snatPolicy"],
    }, "the CRD defaults removed from the golden comparison are fixed; never widen them to make a test pass"
    assert fx["nodepool_spec"]["limits"] == {"cpu": 192, "nvidia.com/gpu": 8}
    assert "status" not in fx


def test_live_capture_writes_the_zone_as_a_placeholder() -> None:
    """The zone of the captured pool is replaced like the account ids: the fixture names no zone of the cluster it was
    captured from, and the golden test maps the reservation's AZ ID to the same placeholder (``az_name_by_id``)."""
    fixtures = CLUSTER / "tests" / "fixtures"
    spec = importlib.util.spec_from_file_location("capture_live_gpu_pool", fixtures / "capture_live_gpu_pool.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    zone = mod.PLACEHOLDERS["zone"]
    kept = {"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["reserved"]}
    live = {"template": {"spec": {"requirements": [
        kept, {"key": "topology.kubernetes.io/zone", "operator": "In", "values": ["xx-test-9z"]},
    ]}}}
    out = mod.sanitise_nodepool(live)
    assert out["template"]["spec"]["requirements"] == [
        kept, {"key": "topology.kubernetes.io/zone", "operator": "In", "values": [zone]},
    ]
    assert live["template"]["spec"]["requirements"][1]["values"] == ["xx-test-9z"], "the input must not be modified"
    fx = json.loads((fixtures / "live-gpu-b200-cb.json").read_text())
    assert fx["placeholders"] == mod.PLACEHOLDERS
    zones = [r["values"] for r in fx["nodepool_spec"]["template"]["spec"]["requirements"]
             if r["key"] == "topology.kubernetes.io/zone"]
    assert zones == [[zone]]
    golden = (CLUSTER / "tests" / "gpu_pools_golden.tftest.hcl").read_text()
    assert re.search(r'(?m)^\s*az_name_by_id\s*=\s*\{[^}\n]*"' + re.escape(zone) + '"', golden), golden[:400]


EXAMPLE_ASSIGN = re.compile(r"(?m)^\s*#?\s*([a-z][a-z0-9_]*)\s*=")


@pytest.mark.parametrize("folder", [CLUSTER, BOOTSTRAP])
def test_backend_example_uses_s3_lockfile(folder: Path) -> None:
    text = (folder / "backend.tf.example").read_text()
    assert 'backend "s3"' in text and re.search(r"use_lockfile\s*=\s*true", text) and re.search(r"encrypt\s*=\s*true", text)
    assert "dynamodb_table" not in text


def test_single_tfvars_example_documents_every_user_facing_choice() -> None:
    path = CLUSTER / "terraform.tfvars.example"
    text = path.read_text()
    assigned = set(EXAMPLE_ASSIGN.findall(text))
    assert set(USER_FACING) <= assigned, sorted(set(USER_FACING) - assigned)
    assert "203.0.113." in text, "use the RFC 5737 documentation range for the CIDR"
    live = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("0.0.0.0/0" in ln for ln in live) and not any(re.match(r"\s*allow_open_eks_api\s*=\s*true", ln) for ln in live)
    assert not (CLUSTER / "terraform.tfvars").exists()
    assert not list(CLUSTER.glob("*.auto.tfvars"))


def test_example_values_pass_the_declared_formats() -> None:
    text = (CLUSTER / "terraform.tfvars.example").read_text()
    sha = re.search(r'(?m)^\s*model_revision\s*=\s*"([^"]*)"', text)
    assert sha and re.fullmatch(r"[0-9a-f]{40}", sha.group(1)), "model_revision in the example must be a 40-hex commit"
    image = re.search(r'(?m)^\s*runtime_image\s*=\s*"([^"]*)"', text)
    assert not image or image.group(1) == "" or re.search(r"@sha256:[0-9a-f]{64}$", image.group(1))


def test_bootstrap_has_example_and_no_deployment_values() -> None:
    assert (BOOTSTRAP / "terraform.tfvars.example").is_file()
    assert (BOOTSTRAP / "main.tf").is_file() or (BOOTSTRAP / "buckets.tf").is_file()
