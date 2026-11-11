# Offline contract tests

The tests are the executable contract of the sample: the manifests, the scripts, the Terraform, the dashboards and the
published tree. They need no AWS account, no credentials and no cluster. When a test fails, fix the artifact, never the
test.

## Suites

| Suite | What it checks |
|---|---|
| `tests/manifests` | The eight `examples/*/manifest.yaml` (the DynamoGraphDeployment CRD at v1.6.1, the digest of their parsed content, the recorded reference cells), pod security, the Kustomize render of the node-ops objects. |
| `tests/scripts` | The scripts of `scripts/` run with stub `kubectl`, `aws`, `curl` and friends first on `PATH`, plus `gen_env.py`, the staging script and the docs of the optimization guides. |
| `tests/terraform` | Static checks of `terraform/cluster` and `terraform/bootstrap`, `terraform fmt` / `validate` and `terraform test` (the `.tftest.hcl` files in `terraform/cluster/tests` and `terraform/bootstrap/tests`). |
| `tests/publish` | The published tree: no account IDs, personal paths or internal hosts, the docs set, the MIT-0 header of every `.py`, `.sh` and `.tf` file, a clean-clone render. |
| `tests/dashboards` | The Grafana dashboards of `dashboards/` against the metric allowlists and the vendored Dynamo dashboards. |

When you edit an `examples/*/manifest.yaml` on purpose, update its parsed-content digest in `FROZEN_EXAMPLES` of
`tests/manifests/lib_frozen.py`.

The staging tool carries its own suite: `tools/stage_model/tests`.

## Run

From the repository root, one suite at a time:

```bash
uv run --with-requirements requirements-test.txt pytest tests/manifests -q
uv run --with-requirements requirements-test.txt pytest tests/scripts -q
uv run --with-requirements requirements-test.txt pytest tests/terraform -q
uv run --with-requirements requirements-test.txt pytest tests/publish -q
uv run --with-requirements requirements-test.txt pytest tests/dashboards -q
uv run --with pytest==9.1.1 --with huggingface_hub==2.3.0 --with boto3==1.43.118 pytest tools/stage_model/tests -q
```

`requirements-test.txt` pins pytest, PyYAML, jsonschema, defusedxml and every package they pull in to the exact
versions the suites are tested with. The `tools/stage_model` command pins its packages inline: pytest at the version of that file,
huggingface_hub and boto3 at those of `tools/stage_model/requirements-cluster.txt`.

Run the suites sequentially, not in parallel: each session writes `manifests/config/platform.env` from
`manifests/config/platform.env.example` when the file is absent and deletes it at the end, so a second session running
at the same time loses the file. Add `-m "not slow"` to skip the slow tests (`terraform test`, `terraform validate`,
checkov).

## The placeholder env

Every render is computed from the placeholder values of
`manifests/config/platform.env.example` (account 123456789012). A fresh clone has no generated
`manifests/config/platform.env`, so the suites use the example. If you ran `scripts/gen_env.py`, your generated file
holds your account values: pytest prints a warning in its header and the tests that pin the placeholder values fail. Run the suites in a clean clone,
or move the generated file aside first.

## Tools and optional inputs

- Always: `git`, `bash`, `jq`, `python3`, [`uv`](https://docs.astral.sh/uv/) and `kubectl` (for `kubectl kustomize`).
- `shellcheck`: the lint tests skip with a reason when it is absent. `checkov`: optional, skipped when absent.
- Terraform >= 1.12 (tested with 1.12.2 and 1.17.0), from `TERRAFORM_BIN` or `PATH`; with an older binary or none the Terraform
  tests skip. `terraform test` mocks every provider, the aliased `aws.us_east_1` included, so it runs without
  credentials. Only `terraform init` needs network access, to download the providers from the registry.
- `DYNAMO_REPO`: a checkout of the Dynamo repository with the tag v1.6.1 (default: a sibling directory named `dynamo`).
  The tests read the DynamoGraphDeployment CRD, the vendored dashboards and the metrics catalogue from it with
  `git show`. In `tests/manifests`, `tests/publish` and `tests/dashboards` they skip without it, with a reason.
- `MANIFESTS_DIR` points `tests/manifests` at another tree; `SCRIPTS_DIR` points `tests/scripts` at another one.

## Conventions

- Scripts are executed through their shebang with stub binaries first on `PATH` and an empty temporary directory as
  the working directory; they locate `manifests/` from their own path. The stubs log their argv as JSON lines, and
  `sleep` advances a fake clock that `date +%s` honours.
- Helper modules (`tests/manifests/lib_manifests.py`, `tests/scripts/lib_scripts.py`) are imported by plain module
  name: pytest puts each test directory on `sys.path`.
- New tests start child processes through `tests/lib_proc.py` (`run`, `start`), not `subprocess`: the command is a
  list of `str` and never goes through a shell. XML is parsed with
  `tests/publish/lib_xml.py`, which parses with defusedxml (pinned in
  `requirements-test.txt`) and refuses entity declarations; no test
  imports the standard library `xml` package.
- Files that would otherwise carry a forbidden literal (an e-mail, a personal path) build it from fragments, as
  `tests/publish/sanitize_scan.py` does. An account ID is the exception: a test that needs one uses the placeholder
  `123456789012` or, where the scanner must flag it, the synthetic `FAKE_ACCOUNT` (1111-2222-3333) of
  `tests/publish/test_sanitization_guards.py`, never a real one, and that file fails on any other account ID built
  from fragments in tracked Python.
- A `.py`, `.sh` or `.tf` file without the MIT-0 header must be listed in `tests/publish/spdx_skip.txt` with the
  reason (vendored or generated files only).
