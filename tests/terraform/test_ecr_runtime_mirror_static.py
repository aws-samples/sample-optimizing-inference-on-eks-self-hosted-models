# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: the ``<project_name>/sglang-runtime`` ECR repository is optional (``create_runtime_mirror_repository``).

The sample runs the public NGC image of ``runtime_image`` directly; the repository only serves the optional copy of
docs/DEPLOY.md ("Mirror the image to your ECR"). The plan-level proof is
``terraform/cluster/tests/ecr_runtime_mirror.tftest.hcl``. Here, statically:

* the variable is a non-nullable bool that defaults to ``false``;
* ``aws_ecr_repository.sglang_runtime`` and ``aws_ecr_lifecycle_policy.sglang_runtime`` are counted on it, while the
  bench repository, its lifecycle policy, the KMS key and its alias are always created (bench needs the key);
* no ``.tf`` file references the optional repository without an index, and the source-region read of the image mirror
  names ``sglang-runtime`` only through the variable;
* the generated-env contract: ``SGLANG_RUNTIME_REPO`` stays a key of ``platform_env``, empty without the repository,
  and the output ``sglang_runtime_ecr_repository_url`` says it is null then; ``platform.env.example`` (the default
  stack) carries the key empty;
* the docs and the example tfvars say how to turn it on: terraform/README.md (variables table, run order, generated
  env), docs/DEPLOY.md (set the variable and apply before reading the output), docs/TROUBLESHOOTING.md, README.md and
  the header of ecr.tf.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hcl import blocks, read_tf

REPO = Path(__file__).resolve().parents[2]
CLUSTER = REPO / "terraform" / "cluster"
VAR = "create_runtime_mirror_repository"
COUNT_RE = re.compile(rf"(?m)^\s*count\s*=\s*var\.{VAR}\s*\?\s*1\s*:\s*0\s*$")
OUTPUT = "sglang_runtime_ecr_repository_url"


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"{rel} missing"
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    """The body of the ``##`` section whose title contains ``heading`` (up to the next ``#`` or ``##`` heading)."""
    m = re.search(rf"(?m)^## .*{re.escape(heading)}.*$", text)
    assert m, f"no '## ...{heading}...' section"
    rest = text[m.end():]
    end = re.search(r"(?m)^#{1,2} ", rest)
    return rest[: end.start()] if end else rest


@pytest.fixture(scope="module")
def tf() -> dict[str, str]:
    return read_tf(CLUSTER)


@pytest.fixture(scope="module")
def resources(tf: dict[str, str]) -> dict[str, str]:
    return blocks("\n".join(tf.values()), "resource", 2)


# ----------------------------------------------------------------------------------- the variable
def test_variable_is_a_non_nullable_bool_defaulting_to_false(tf: dict[str, str]) -> None:
    body = blocks(tf["variables.tf"], "variable").get(VAR, "")
    assert body, f"variables.tf declares {VAR}"
    assert re.search(r"(?m)^\s*type\s*=\s*bool\s*$", body)
    assert re.search(r"(?m)^\s*default\s*=\s*false\s*$", body), "off by default: the sample runs the public NGC image"
    assert re.search(r"(?m)^\s*nullable\s*=\s*false\s*$", body)
    assert re.search(r"(?m)^\s*description\s*=", body)


# ----------------------------------------------------------------------------------- what is counted
@pytest.mark.parametrize("name", ["aws_ecr_repository.sglang_runtime", "aws_ecr_lifecycle_policy.sglang_runtime"])
def test_the_runtime_repository_and_its_policy_are_counted_on_the_variable(resources: dict[str, str], name: str) -> None:
    assert COUNT_RE.search(resources[name]), f"{name}: count = var.{VAR} ? 1 : 0"


def test_the_lifecycle_policy_targets_the_counted_repository(resources: dict[str, str]) -> None:
    body = resources["aws_ecr_lifecycle_policy.sglang_runtime"]
    assert re.search(r"repository\s*=\s*aws_ecr_repository\.sglang_runtime\[0\]\.name", body), body


@pytest.mark.parametrize(
    "name", ["aws_ecr_repository.bench", "aws_ecr_lifecycle_policy.bench", "aws_kms_key.ecr", "aws_kms_alias.ecr"]
)
def test_bench_and_the_key_are_always_created(resources: dict[str, str], name: str) -> None:
    assert name in resources
    assert not re.search(r"(?m)^\s*(count|for_each)\s*=", resources[name]), f"{name} is not conditional"


def test_no_file_references_the_optional_repository_without_an_index(tf: dict[str, str]) -> None:
    bad = [f"{f}: {m.group(0)}" for f, text in tf.items()
           for m in re.finditer(r"aws_ecr_(?:repository|lifecycle_policy)\.sglang_runtime\.\w+", text)]
    assert not bad, f"index the counted resources ([0] or [*]): {bad}"


def test_the_image_mirror_reads_the_source_runtime_repository_only_with_the_variable(tf: dict[str, str]) -> None:
    text = tf["iam-image-mirror.tf"]
    m = re.search(r"image_mirror_source_repo_arns\s*=(.*?)\n\s*\]\n", text, re.DOTALL)
    assert m, "local.image_mirror_source_repo_arns"
    expr = m.group(1)
    assert "sglang-runtime" in expr and f"var.{VAR}" in expr, expr
    assert not re.search(r'\[\s*"bench"\s*,\s*"sglang-runtime"\s*\]', expr), "sglang-runtime is not read unconditionally"


# ----------------------------------------------------------------------------------- generated env contract
def test_the_env_key_stays_and_falls_back_to_empty(tf: dict[str, str]) -> None:
    line = next((ln for ln in tf["outputs.tf"].splitlines() if re.match(r"\s*SGLANG_RUNTIME_REPO\s*=", ln)), "")
    assert line, "platform_env keeps the SGLANG_RUNTIME_REPO key"
    assert f"var.{VAR}" in line and '""' in line, f"empty without the repository: {line.strip()}"


def test_the_output_says_it_is_null_without_the_repository(tf: dict[str, str]) -> None:
    body = blocks(tf["outputs.tf"], "output").get(OUTPUT, "")
    assert body, f"output {OUTPUT} stays"
    desc = re.search(r'description\s*=\s*"([^"]*)"', body)
    assert desc and "null" in desc.group(1) and VAR in desc.group(1), desc


def test_platform_env_example_carries_the_key_empty() -> None:
    lines = _read("manifests/config/platform.env.example").splitlines()
    assert "SGLANG_RUNTIME_REPO=" in lines, "the example is the default stack: the key is there, with no value"


# ----------------------------------------------------------------------------------- docs and examples
def test_tfvars_example_shows_the_switch_next_to_the_mirrored_runtime_image() -> None:
    text = _read("terraform/cluster/terraform.tfvars.example")
    m = re.search(rf"(?m)^#?\s*{VAR}\s*=\s*(true|false)\s*$", text)
    assert m, f"terraform.tfvars.example shows {VAR}"
    assert text.index(m.group(0)) < text.index("runtime_image = "), "explained before the runtime_image it serves"
    assert not re.search(rf"(?m)^{VAR}\s*=\s*true", text), "the example keeps the default (off)"


def test_terraform_readme_documents_the_variable_and_the_empty_key() -> None:
    text = _read("terraform/README.md")
    rows = [ln for ln in text.splitlines() if ln.startswith(f"| `{VAR}` |")]
    assert len(rows) == 1, f"terraform/README.md variables table: one row for {VAR}"
    assert "| `false` |" in rows[0] and "docs/DEPLOY.md" in rows[0], rows[0]
    run_order = _flat(_section(text, "Run order"))
    assert f"`{VAR} = true`" in run_order, "the optional copy step sets the variable first"
    env = _flat(_section(text, "Generated env"))
    assert re.search(r"`SGLANG_RUNTIME_REPO`[^.]*empty", env), "the generated env says when SGLANG_RUNTIME_REPO is empty"


def test_deploy_mirror_section_turns_the_repository_on_before_reading_its_output() -> None:
    sec = _section(_read("docs/DEPLOY.md"), "Mirror the image to your ECR")
    switch = sec.find(f"{VAR} = true")
    assert switch >= 0, f"the mirror section says to set {VAR} = true"
    apply = sec.find("terraform -chdir=terraform/cluster apply", switch)
    read = sec.find(f"output -raw {OUTPUT}")
    assert 0 <= switch < apply < read, "set the variable, apply, then read the repository URL"
    assert not re.search(r"(?i)the stack creates a `?sglang-runtime`? ECR repository", sec), "it is not created by default"


def test_troubleshooting_covers_the_missing_output_and_an_existing_repository() -> None:
    text = _read("docs/TROUBLESHOOTING.md")
    assert f'Output "{OUTPUT}" not found' in text, "the error of terraform output -raw without the repository"
    assert VAR in text and re.search(r"(?i)destroy", text)


def test_readme_and_the_ecr_header_say_the_runtime_repository_is_optional() -> None:
    readme = _flat(_read("README.md"))
    assert "two ECR repositories" not in readme
    assert re.search(r"(?i)optional[^.]*ECR repository|ECR repository[^.]*optional", readme)
    header = (CLUSTER / "ecr.tf").read_text(encoding="utf-8").split("####\n\nlocals", 1)[0]
    assert VAR in header and re.search(r"(?i)optional", header), "the ecr.tf header says when sglang-runtime exists"
    assert "Both repositories are encrypted" not in header


def test_tftest_file_exists() -> None:
    assert (CLUSTER / "tests" / "ecr_runtime_mirror.tftest.hcl").is_file()
