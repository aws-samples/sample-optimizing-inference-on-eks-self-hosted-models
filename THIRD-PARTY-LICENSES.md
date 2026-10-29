# Third-party licenses

The code and documentation written for this sample are licensed under the MIT-0 License: see [LICENSE](LICENSE).
This file lists, with their licenses:

1. the third-party material that this repository contains, copied or derived from another project;
2. the third-party software that the sample pulls at build, deploy or run time and does not contain or redistribute.

The attribution notices of the Apache-2.0 material of the first part are in [NOTICE](NOTICE). The logos and icons of
the architecture diagram are listed, with their sources and terms, in [docs/static/LICENSES.md](docs/static/LICENSES.md)
and are not repeated here.

Each license below was read from the project's own license file, chart annotation, model card or package metadata
on 2026-10-26, and on 2026-10-27 for the packages of `tests/requirements-test.txt` (their PyPI metadata; for
defusedxml also the LICENSE file of its wheel). A component marked "not verified" was not confirmed that way, and no
license is claimed for it.

## Third-party material in this repository

| Component | In this repository | Upstream | License | Changes |
|---|---|---|---|---|
| NVIDIA Dynamo dashboard: frontend and worker overview | `dashboards/dynamo/grafana-dynamo-dashboard-configmap.yaml` | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo), tag v1.5.1 (commit 2093662a4d4e5fd318732935213dde08dc7be818), `deploy/observability/grafana-dynamo-dashboard-configmap.yaml` | Apache-2.0 | Unmodified: a provenance comment was added below the upstream SPDX lines; everything from `apiVersion:` on is byte-identical |
| NVIDIA Dynamo dashboard: disaggregated serving | `dashboards/dynamo/grafana-disagg-dashboard-configmap.yaml` | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo), tag v1.5.1, `deploy/observability/grafana-disagg-dashboard-configmap.yaml` | Apache-2.0 | Unmodified: a provenance comment was added below the upstream SPDX lines; everything from `apiVersion:` on is byte-identical |
| SGLang engine dashboard of NVIDIA Dynamo | `dashboards/sglang/sglang.json` | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo), tag v1.5.1, `dev/observability/grafana_dashboards/sglang.json` | Apache-2.0 | Modified: the changes are listed in [dashboards/README.md](dashboards/README.md#provenance-and-licence) and in the dashboard `description` |
| NVIDIA Dynamo DCGM metrics list with NVLink profiling | `terraform/cluster/observability/dcgm-metrics-with-nvlink.csv` | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo), tag v1.5.1 (commit 2093662a4d4e5fd318732935213dde08dc7be818), `deploy/observability/dcgm-metrics-with-nvlink.csv`. The Dynamo file is `etc/dcp-metrics-included.csv` of [NVIDIA/dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter) (Apache-2.0), as in its tags 4.3.1-4.4.0 to 4.4.2-4.7.0, with a 4-line "NVLink Profiling Metrics" block added by Dynamo | Apache-2.0 | Unmodified: a provenance comment was added at the top; everything from `# Format` on is byte-identical. It is the `customMetrics` list of the dcgm-exporter chart (`terraform/cluster/observability.tf`) |
| NVIDIA DCGM Exporter dashboard | `dashboards/dcgm/dcgm-grafana-dashboard.json` | [NVIDIA/dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter), `grafana/dcgm-exporter-dashboard.json` as last changed in commit 68419baf4a227f202ed8b6f79246811971352298 (2020-10-03), the same file in tags 2.2.9-2.4.1 to 2.4.7-2.6.11 | Apache-2.0 | Modified: the `__inputs` block (the import prompt for `DS_PROMETHEUS`) was removed, the 10 datasource references `${DS_PROMETHEUS}` were replaced by the datasource uid `prometheus`, and the final newline was dropped. Panels and queries are unchanged |
| Agentic request trace of the NVIDIA Dynamo recipes | `tools/bench-image/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl` | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo), `recipes/kimi-k2.6/perf/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl`, added in commit 66a95d5aa3b5d238fd94b6f8243c4e479cb7242c and unchanged in tag v1.5.1 | Apache-2.0 | Modified (2026-10-18): the 48 rows with `input_length + output_length >= 500000` were removed; the other 3493 rows are unchanged. Checksums and the command that reproduces it: [tools/bench-image/traces/README.md](tools/bench-image/traces/README.md) |
| Qwen3-32B model configuration (excerpt) | `tools/demo_bench/tests/fixtures/qwen3_32b_config.json` | [Qwen/Qwen3-32B](https://huggingface.co/Qwen/Qwen3-32B), revision 9216db5781bf21249d130ec9da846c4624c16137, `config.json` | Apache-2.0 | Excerpt for a unit test of the KV sizing: 11 of the 26 keys, values unchanged |
| W2 agentic test trace | `tools/demo_bench/tests/fixtures/w2_qwen38/window.jsonl` | Generated for this sample by `demo-bench trace w2` ([tools/demo_bench/README.md](tools/demo_bench/README.md)) with `--osl-file` taken from [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo), `recipes/kimi-k2.6/perf/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl` as added in commit 66a95d5aa3b5d238fd94b6f8243c4e479cb7242c (the upstream file of the agentic request trace above, all 3541 rows; unchanged in tag v1.5.1) | Apache-2.0 | Derived (2026-10-22): every `output_length` is one of the upstream `output_length` values, capped at 12000; sessions, prefixes, input lengths and timestamps are synthetic. The command, seed and sha256 values that reproduce it byte for byte are recorded in [tools/demo_bench/tests/README.md](tools/demo_bench/tests/README.md) and replayed by its provenance test |

The dashboards above are JSON documents that Grafana loads. This repository contains no Grafana source code, image or
binary: Grafana is deployed from its upstream image by the kube-prometheus-stack chart and is listed in the second part.

## Third-party software used but not contained

The sample pulls the components below from their upstream registries when the reader builds the bench image, applies
Terraform, stages the model or runs the tools. This repository does not contain or redistribute them, and each one
comes with its own license and terms. The version is the one pinned in the named file. Dependencies that the
repository does not pin (the packages that aiperf, boto3 and huggingface_hub pull in, the images that the charts
choose) are resolved at install time and are not listed; `pip-licenses`, run in the environment that installed them,
lists the Python ones.

### Container images and Helm charts

| Component | In this repository | Version | License | Notes |
|---|---|---|---|---|
| NVIDIA Dynamo SGLang runtime image | `versions.env`, `terraform/cluster/terraform.tfvars.example` | `nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0-efa`, pinned by digest | not verified | Published by NVIDIA on NGC (Dynamo 1.6.0, SGLang 0.5.21, NIXL 1.5.0, EFA installer 1.50.0). The cluster pulls it; this repository never rebuilds or redistributes it. Its terms are those of its NGC catalog entry |
| dynamo-platform | `terraform/cluster/dynamo-platform.tf`, `terraform/cluster/variables.tf` | 1.5.1 | Apache-2.0 | Helm chart of the Dynamo operator, downloaded from helm.ngc.nvidia.com. Chart source: [deploy/helm/charts/platform](https://github.com/ai-dynamo/dynamo/tree/v1.5.1/deploy/helm/charts/platform) at tag v1.5.1 (SPDX header of its Chart.yaml). Only the operator is installed: etcd, NATS, Grove and the KAI scheduler are off |
| NVIDIA Dynamo operator image | `terraform/cluster/dynamo-platform.tf` | set by the dynamo-platform chart 1.5.1 | not verified | NVIDIA container image on NGC that the chart deploys |
| kube-prometheus-stack | `terraform/cluster/observability.tf`, `terraform/cluster/variables.tf` | 69.7.4 | Apache-2.0 | Helm chart of [prometheus-community/helm-charts](https://github.com/prometheus-community/helm-charts/tree/kube-prometheus-stack-69.7.4/charts/kube-prometheus-stack): repository LICENSE and chart annotation `artifacthub.io/license` |
| Grafana server | `terraform/cluster/observability.tf` | set by the grafana subchart (8.10.x) of kube-prometheus-stack 69.7.4 | AGPL-3.0-only | Default license of [grafana/grafana](https://github.com/grafana/grafana/blob/main/LICENSING.md) (read at tag v11.5.2 and on the default branch). The chart deploys the upstream image; Grafana is not redistributed by this repository, which contains no Grafana code |
| Other images of kube-prometheus-stack | `terraform/cluster/observability.tf` | set by kube-prometheus-stack 69.7.4 | not verified | Prometheus, Alertmanager, Prometheus Operator, kube-state-metrics, node-exporter and the Grafana dashboard sidecar, pulled from their upstream registries by the chart |
| dcgm-exporter | `terraform/cluster/observability.tf`, `terraform/cluster/variables.tf` | 4.8.4 | Apache-2.0 | Helm chart of [NVIDIA/dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter/tree/4.8.4/deployment), tag 4.8.4 (repository LICENSE) |
| NVIDIA DCGM Exporter image | `terraform/cluster/observability.tf` | set by the dcgm-exporter chart 4.8.4 | not verified | NVIDIA container image that the chart deploys |

### Terraform providers and modules

| Component | In this repository | Version | License | Notes |
|---|---|---|---|---|
| hashicorp/aws | `terraform/cluster/.terraform.lock.hcl`, `terraform/bootstrap/.terraform.lock.hcl` | 6.70.0 | MPL-2.0 | [LICENSE](https://github.com/hashicorp/terraform-provider-aws/blob/v6.70.0/LICENSE) at tag v6.70.0 |
| hashicorp/helm | `terraform/cluster/.terraform.lock.hcl` | 2.17.0 | MPL-2.0 | [LICENSE](https://github.com/hashicorp/terraform-provider-helm/blob/v2.17.0/LICENSE) at tag v2.17.0 |
| hashicorp/kubernetes | `terraform/cluster/.terraform.lock.hcl` | 2.35.1 | MPL-2.0 | [LICENSE](https://github.com/hashicorp/terraform-provider-kubernetes/blob/v2.35.1/LICENSE) at tag v2.35.1 |
| hashicorp/tls | `terraform/cluster/.terraform.lock.hcl` | 4.4.2 | MPL-2.0 | [LICENSE](https://github.com/hashicorp/terraform-provider-tls/blob/v4.4.2/LICENSE) at tag v4.4.2 |
| hashicorp/cloudinit | `terraform/cluster/.terraform.lock.hcl` | 2.4.1 | MPL-2.0 | [LICENSE](https://github.com/hashicorp/terraform-provider-cloudinit/blob/v2.4.1/LICENSE) at tag v2.4.1; required by the EKS module |
| hashicorp/null | `terraform/cluster/.terraform.lock.hcl` | 3.3.2 | MPL-2.0 | [LICENSE](https://github.com/hashicorp/terraform-provider-null/blob/v3.3.2/LICENSE) at tag v3.3.2; required by the EKS module |
| hashicorp/time | `terraform/cluster/.terraform.lock.hcl` | 0.14.2 | MPL-2.0 | [LICENSE](https://github.com/hashicorp/terraform-provider-time/blob/v0.14.2/LICENSE) at tag v0.14.2; required by the EKS module |
| alekc/kubectl | `terraform/cluster/.terraform.lock.hcl` | 2.1.3 | MPL-2.0 | [LICENSE](https://github.com/alekc/terraform-provider-kubectl/blob/v2.1.3/LICENSE) at tag v2.1.3 |
| terraform-aws-modules/eks/aws | `terraform/cluster/eks.tf` | 21.15.1 | Apache-2.0 | [LICENSE](https://github.com/terraform-aws-modules/terraform-aws-eks/blob/v21.15.1/LICENSE) at tag v21.15.1 |
| terraform-aws-modules/vpc/aws | `terraform/cluster/vpc.tf` | 6.6.0 | Apache-2.0 | [LICENSE](https://github.com/terraform-aws-modules/terraform-aws-vpc/blob/v6.6.0/LICENSE) at tag v6.6.0 |

### Bench image

The only image this repository builds is the bench image of `tools/bench-image/Dockerfile`; the reader builds and
pushes it to their own registry. Its Python packages are in the next table.

| Component | In this repository | Version | License | Notes |
|---|---|---|---|---|
| Python base image | `tools/bench-image/Dockerfile` | `python:3.12.16-slim-trixie`, pinned by digest | not verified | Docker Official Image (CPython on Debian); the image carries many packages, each under its own terms |
| Debian packages of the image | `tools/bench-image/Dockerfile` | from the Debian release of the base image | not verified | bash, jq and ca-certificates; gnupg and unzip in a build stage only |
| kubectl | `tools/bench-image/Dockerfile` | v1.35.10 | Apache-2.0 | [LICENSE](https://github.com/kubernetes/kubernetes/blob/v1.35.10/LICENSE) of kubernetes/kubernetes at tag v1.35.10; checksum-verified download |
| AWS CLI | `tools/bench-image/Dockerfile` | 2.38.1 | Apache-2.0 | [LICENSE.txt](https://github.com/aws/aws-cli/blob/2.38.1/LICENSE.txt) of aws/aws-cli at tag 2.38.1; signature- and checksum-verified bundled installer |

### Python packages

`tools/demo_bench` has no runtime dependency: its `plot` extra and its `dev` group are pinned in
`tools/demo_bench/uv.lock`. `tools/matrix_driver` uses the standard library only. The test suites install pytest,
PyYAML, jsonschema, defusedxml and every package they pull in from `tests/requirements-test.txt`, which pins each one
to an exact version (`uv run --with-requirements tests/requirements-test.txt`); `scripts/preflight-flags.sh` pins the
same PyYAML.

| Component | In this repository | Version | License | Notes |
|---|---|---|---|---|
| [aiperf](https://pypi.org/project/aiperf/) | `tools/bench-image/Dockerfile` | 0.13.0 | Apache-2.0 | Bench image |
| [ast-serialize](https://pypi.org/project/ast-serialize/) | `tools/demo_bench/uv.lock` | 0.13.1 | MIT | dev group (mypy) |
| [attrs](https://pypi.org/project/attrs/) | `tests/requirements-test.txt` | 26.1.0 | MIT | test suites (jsonschema, referencing) |
| [boto3](https://pypi.org/project/boto3/) | `tools/stage_model/requirements.txt` | 1.43.118 | Apache-2.0 | Model-stage Job |
| [certifi](https://pypi.org/project/certifi/) | `tools/bench-image/Dockerfile` | 2026.10.23 | MPL-2.0 | Bench image; at or above the fixed release 2026.10.19, asserted at build time |
| [colorama](https://pypi.org/project/colorama/) | `tools/demo_bench/uv.lock`, `tests/requirements-test.txt` | 0.4.6 | BSD-3-Clause | dev group and test suites (pytest, Windows only) |
| [contourpy](https://pypi.org/project/contourpy/) | `tools/demo_bench/uv.lock` | 1.3.3, 1.4.0 | BSD-3-Clause | plot extra (matplotlib); one version per Python range |
| [cycler](https://pypi.org/project/cycler/) | `tools/demo_bench/uv.lock` | 0.12.1 | BSD-3-Clause | plot extra (matplotlib) |
| [defusedxml](https://pypi.org/project/defusedxml/) | `tools/demo_bench/uv.lock`, `tests/requirements-test.txt` | 0.7.1 | PSF-2.0 | dev group and test suites: the XML parser of `tests/publish/lib_xml.py` and `tools/demo_bench/tests/lib_xml.py`. Python Software Foundation License Version 2 (its LICENSE file; metadata `License: PSFL`) |
| [fonttools](https://pypi.org/project/fonttools/) | `tools/demo_bench/uv.lock` | 4.67.0 | MIT | plot extra (matplotlib) |
| [hatchling](https://pypi.org/project/hatchling/) | `tools/demo_bench/pyproject.toml` | 1.32.4 | MIT | Build backend of demo_bench |
| [huggingface_hub](https://pypi.org/project/huggingface-hub/) | `tools/stage_model/requirements.txt` | 2.3.0 | Apache-2.0 | Model-stage Job |
| [hypothesis](https://pypi.org/project/hypothesis/) | `tools/demo_bench/uv.lock` | 6.170.1 | MPL-2.0 | dev group |
| [iniconfig](https://pypi.org/project/iniconfig/) | `tools/demo_bench/uv.lock`, `tests/requirements-test.txt` | 2.3.1 | MIT | dev group and test suites (pytest) |
| [jsonschema](https://pypi.org/project/jsonschema/) | `tests/requirements-test.txt` | 4.26.0 | MIT | test suites |
| [jsonschema-specifications](https://pypi.org/project/jsonschema-specifications/) | `tests/requirements-test.txt` | 2025.9.1 | MIT | test suites (jsonschema) |
| [kiwisolver](https://pypi.org/project/kiwisolver/) | `tools/demo_bench/uv.lock` | 1.5.1 | BSD-3-Clause | plot extra (matplotlib) |
| [librt](https://pypi.org/project/librt/) | `tools/demo_bench/uv.lock` | 0.16.0 | MIT | dev group (mypy) |
| [matplotlib](https://pypi.org/project/matplotlib/) | `tools/demo_bench/uv.lock`, `tools/demo_bench/pyproject.toml` | 3.11.2 | LicenseRef-Matplotlib | plot extra and dev group. The PSF-style license agreement of the Matplotlib Development Team, which has no SPDX License List id; the package also carries the licenses of its bundled fonts and code |
| [mypy](https://pypi.org/project/mypy/) | `tools/demo_bench/uv.lock` | 2.4.0 | MIT | dev group |
| [mypy-extensions](https://pypi.org/project/mypy-extensions/) | `tools/demo_bench/uv.lock` | 1.1.0 | MIT | dev group (mypy) |
| [numpy](https://pypi.org/project/numpy/) | `tools/demo_bench/uv.lock` | 2.4.6, 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | plot extra (matplotlib); one version per Python range. The expression is the package's own, covering its bundled code |
| [packaging](https://pypi.org/project/packaging/) | `tools/demo_bench/uv.lock`, `tests/requirements-test.txt` | 26.3 | Apache-2.0 OR BSD-2-Clause | plot extra, dev group and test suites (pytest) |
| [pathspec](https://pypi.org/project/pathspec/) | `tools/demo_bench/uv.lock` | 1.1.1 | MPL-2.0 | dev group (mypy) |
| [pillow](https://pypi.org/project/pillow/) | `tools/demo_bench/uv.lock`, `tools/bench-image/Dockerfile` | 13.0.0 | MIT-CMU | plot extra (matplotlib); bench image, uv override of the pillow~=12.3.0 of aiperf, at the fixed release 13.0.0, asserted at build time |
| [pip](https://pypi.org/project/pip/) | `tools/bench-image/Dockerfile` | 26.2.2 | MIT | Bench image installer; the matrix driver installs its wheel with it at run time. Its vendored libraries (`pip/_vendor`) ship with it under their own licences |
| [pluggy](https://pypi.org/project/pluggy/) | `tools/demo_bench/uv.lock`, `tests/requirements-test.txt` | 1.7.0 | MIT | dev group and test suites (pytest) |
| [pygments](https://pypi.org/project/pygments/) | `tools/demo_bench/uv.lock`, `tests/requirements-test.txt` | 2.21.0 | BSD-2-Clause | dev group and test suites (pytest) |
| [pyparsing](https://pypi.org/project/pyparsing/) | `tools/demo_bench/uv.lock` | 3.3.3 | MIT | plot extra (matplotlib) |
| [pytest](https://pypi.org/project/pytest/) | `tools/demo_bench/uv.lock`, `tests/requirements-test.txt` | 9.1.1 | MIT | dev group and test suites |
| [python-dateutil](https://pypi.org/project/python-dateutil/) | `tools/demo_bench/uv.lock` | 2.9.0.post0 | Apache-2.0 AND BSD-3-Clause | plot extra (matplotlib). All code is BSD-3-Clause; contributions after 2017-12-01 are also Apache-2.0 |
| [PyYAML](https://pypi.org/project/PyYAML/) | `tests/requirements-test.txt`, `scripts/preflight-flags.sh` | 6.0.3 | MIT | test suites; the PyYAML that `scripts/preflight-flags.sh` adds when python3 has none |
| [referencing](https://pypi.org/project/referencing/) | `tests/requirements-test.txt` | 0.37.0 | MIT | test suites (jsonschema) |
| [rpds-py](https://pypi.org/project/rpds-py/) | `tests/requirements-test.txt` | 2026.9.1 | MIT | test suites (jsonschema, referencing) |
| [ruff](https://pypi.org/project/ruff/) | `tools/demo_bench/uv.lock` | 0.17.1 | MIT | dev group |
| [six](https://pypi.org/project/six/) | `tools/demo_bench/uv.lock` | 1.17.0 | MIT | plot extra (python-dateutil) |
| [sortedcontainers](https://pypi.org/project/sortedcontainers/) | `tools/demo_bench/uv.lock` | 2.4.0 | Apache-2.0 | dev group (hypothesis) |
| [typing-extensions](https://pypi.org/project/typing-extensions/) | `tools/demo_bench/uv.lock`, `tests/requirements-test.txt` | 4.16.0 | PSF-2.0 | dev group (mypy) and test suites (referencing, Python below 3.13) |
| [uv](https://pypi.org/project/uv/) | `tools/bench-image/Dockerfile` | 0.13.5 | MIT OR Apache-2.0 | Bench image build only; uninstalled in the same layer |

### Models

The weights are downloaded by the reader into their own bucket (or read from the Hub at run time) and are not
distributed here.

| Component | In this repository | Version | License | Notes |
|---|---|---|---|---|
| [Qwen/Qwen3.8-27B-FP8](https://huggingface.co/Qwen/Qwen3.8-27B-FP8) | `terraform/cluster/terraform.tfvars.example` | revision 017b9c7af6b5689d5dd426a76e0bc077eb5ca20a | Apache-2.0 | The served model of the examples; license of its model card. Staged by the model-stage Job |
| [Qwen/Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B) | `manifests/stage-mocker/base/dgd.yaml` | default branch, not pinned | Apache-2.0 | Tokenizer of the CPU-only mocker workers; license of its model card |

## License of this repository

Everything in this repository that is not listed in the first part was written for this sample and is licensed under
the MIT-0 License: see [LICENSE](LICENSE).
