# Third-party licenses

The code and documentation written for this sample are licensed under the MIT-0 License: see [LICENSE](LICENSE).
This file lists, with their licenses:

1. the third-party material that this repository contains, copied or derived from another project;
2. the third-party software that the sample pulls at build, deploy or run time and does not contain or redistribute.

The attribution notices of the Apache-2.0 material of the first part are in [NOTICE](NOTICE). The logos and icons of
the architecture diagram are listed, with their sources and terms, in [docs/static/LICENSES.md](docs/static/LICENSES.md)
and are not repeated here.

Each license below was read from the project's own license file, chart annotation, model card or package metadata
on 2026-10-26, and on 2026-10-27 for the packages of `requirements-test.txt` (their PyPI metadata; for
defusedxml also the LICENSE file of its wheel). A component marked "not verified" was not confirmed that way, and no
license is claimed for it.

## Third-party material in this repository

| Component | In this repository | Upstream | License | Changes |
|---|---|---|---|---|
| NVIDIA Dynamo dashboard: frontend and worker overview | `dashboards/dynamo/grafana-dynamo-dashboard-configmap.yaml` | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo), tag v1.6.1 (commit 0d9c24a8bf6db13c1a1223cf15fcbee9140d1ac3), `deploy/observability/grafana-dynamo-dashboard-configmap.yaml` | Apache-2.0 | Unmodified: a provenance comment was added below the upstream SPDX lines; everything from `apiVersion:` on is byte-identical |
| NVIDIA Dynamo dashboard: disaggregated serving | `dashboards/dynamo/grafana-disagg-dashboard-configmap.yaml` | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo), tag v1.6.1, `deploy/observability/grafana-disagg-dashboard-configmap.yaml` | Apache-2.0 | Unmodified: a provenance comment was added below the upstream SPDX lines; everything from `apiVersion:` on is byte-identical |
| SGLang engine dashboard of NVIDIA Dynamo | `dashboards/sglang/sglang.json` | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo), tag v1.6.1, `dev/observability/grafana_dashboards/sglang.json` | Apache-2.0 | Modified: the changes are listed in [dashboards/README.md](dashboards/README.md#provenance-and-licence) and in the dashboard `description` |
| NVIDIA Dynamo DCGM metrics list with NVLink profiling | `terraform/cluster/observability/dcgm-metrics-with-nvlink.csv` | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo), tag v1.6.1 (commit 0d9c24a8bf6db13c1a1223cf15fcbee9140d1ac3), `deploy/observability/dcgm-metrics-with-nvlink.csv`. The Dynamo file is `etc/dcp-metrics-included.csv` of [NVIDIA/dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter) (Apache-2.0), as in its tags 4.3.1-4.4.0 to 4.4.2-4.7.0, with a 4-line "NVLink Profiling Metrics" block added by Dynamo | Apache-2.0 | Unmodified: a provenance comment was added at the top; everything from `# Format` on is byte-identical. It is the `customMetrics` list of the dcgm-exporter chart (`terraform/cluster/observability.tf`) |
| NVIDIA DCGM Exporter dashboard | `dashboards/dcgm/dcgm-grafana-dashboard.json` | [NVIDIA/dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter), `grafana/dcgm-exporter-dashboard.json` as last changed in commit 68419baf4a227f202ed8b6f79246811971352298 (2020-10-03), the same file in tags 2.2.9-2.4.1 to 2.4.7-2.6.11 | Apache-2.0 | Modified: the `__inputs` block (the import prompt for `DS_PROMETHEUS`) was removed, the 10 datasource references `${DS_PROMETHEUS}` were replaced by the datasource uid `prometheus`, and the final newline was dropped. Panels and queries are unchanged |

The dashboards above are JSON documents that Grafana loads. This repository contains no Grafana source code, image or
binary: Grafana is deployed from its upstream image by the kube-prometheus-stack chart and is listed in the second part.

## Third-party software used but not contained

The sample pulls the components below from their upstream registries when the reader applies Terraform, stages the
model or runs the tools. This repository does not contain or redistribute them, and each one comes with its own
license and terms. The version is the one pinned in the named file. Dependencies that the repository does not pin
(the packages that boto3 and huggingface_hub pull in, the images that the charts choose) are resolved at install time and are not listed; `pip-licenses`, run in the environment that installed them,
lists the Python ones.

### Container images and Helm charts

| Component | In this repository | Version | License | Notes |
|---|---|---|---|---|
| NVIDIA Dynamo SGLang runtime image | `versions.env`, `terraform/cluster/terraform.tfvars.example` | `nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.1-efa`, pinned by digest | not verified | Published by NVIDIA on NGC (Dynamo 1.6.1, SGLang 0.5.21, NIXL 1.5.0, EFA installer 1.50.0). The cluster pulls it; this repository never rebuilds or redistributes it. Its terms are those of its NGC catalog entry |
| dynamo-platform | `terraform/cluster/dynamo-platform.tf`, `terraform/cluster/variables.tf` | 1.6.1 | Apache-2.0 | Helm chart of the Dynamo operator, downloaded from helm.ngc.nvidia.com. Chart source: [deploy/helm/charts/platform](https://github.com/ai-dynamo/dynamo/tree/v1.6.1/deploy/helm/charts/platform) at tag v1.6.1 (SPDX header of its Chart.yaml). Only the operator is installed: etcd, NATS, Grove and the KAI scheduler are off |
| NVIDIA Dynamo operator image | `terraform/cluster/dynamo-platform.tf` | set by the dynamo-platform chart 1.6.1 | not verified | NVIDIA container image on NGC that the chart deploys |
| kube-prometheus-stack | `terraform/cluster/observability.tf`, `terraform/cluster/variables.tf` | 69.7.4 | Apache-2.0 | Helm chart of [prometheus-community/helm-charts](https://github.com/prometheus-community/helm-charts/tree/kube-prometheus-stack-69.7.4/charts/kube-prometheus-stack): repository LICENSE and chart annotation `artifacthub.io/license` |
| Grafana server | `terraform/cluster/observability.tf` | set by the grafana subchart (8.10.x) of kube-prometheus-stack 69.7.4 | AGPL-3.0-only | Default license of [grafana/grafana](https://github.com/grafana/grafana/blob/main/LICENSING.md) (read at tag v11.5.2 and on the default branch). The chart deploys the upstream image; Grafana is not redistributed by this repository, which contains no Grafana code |
| Other images of kube-prometheus-stack | `terraform/cluster/observability.tf` | set by kube-prometheus-stack 69.7.4 | not verified | Prometheus, Alertmanager, Prometheus Operator, kube-state-metrics, node-exporter and the Grafana dashboard sidecar, pulled from their upstream registries by the chart |
| dcgm-exporter | `terraform/cluster/observability.tf`, `terraform/cluster/variables.tf` | 4.8.4 | Apache-2.0 | Helm chart of [NVIDIA/dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter/tree/4.8.4/deployment), tag 4.8.4 (repository LICENSE) |
| NVIDIA DCGM Exporter image | `terraform/cluster/observability.tf` | set by the dcgm-exporter chart 4.8.4 | not verified | NVIDIA container image that the chart deploys |
| python image | `benchmarks/perf.yaml` | `python:3.12-slim`, a tag (not pinned by digest) | not verified | Docker Official Image that the benchmark Job pulls; the Job also installs curl and jq from its Debian packages at run time |
| AIPerf | `benchmarks/perf.yaml` | 0.13.0 | Apache-2.0 | [ai-dynamo/aiperf](https://github.com/ai-dynamo/aiperf), installed with pip by the benchmark Job at run time; license from the `License` field of the installed package metadata |
| AWS CLI v2 image | `manifests/node-ops/job-model-prefetch.yaml`, `manifests/node-ops/pod-ephemeral-shell.yaml` | `public.ecr.aws/aws-cli/aws-cli:2.38.1`, pinned by digest | Apache-2.0 | The image of the prefetch Job and of the helper pod. Apache-2.0 is the license of the AWS CLI v2 source ([LICENSE.txt](https://github.com/aws/aws-cli/blob/v2/LICENSE.txt), branch v2); the image also holds the licenses of its base packages: not inventoried |

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

### Python packages

The test suites install pytest, PyYAML, jsonschema, defusedxml and every package they pull in from
`requirements-test.txt`, which pins each one to an exact version
(`uv run --with-requirements requirements-test.txt`).

| Component | In this repository | Version | License | Notes |
|---|---|---|---|---|
| [attrs](https://pypi.org/project/attrs/) | `requirements-test.txt` | 26.1.0 | MIT | test suites (jsonschema, referencing) |
| [boto3](https://pypi.org/project/boto3/) | `tools/stage_model/requirements-cluster.txt` | 1.43.118 | Apache-2.0 | Model-stage Job |
| [colorama](https://pypi.org/project/colorama/) | `requirements-test.txt` | 0.4.6 | BSD-3-Clause | test suites (pytest, Windows only) |
| [defusedxml](https://pypi.org/project/defusedxml/) | `requirements-test.txt` | 0.7.1 | PSF-2.0 | test suites: the XML parser of `tests/publish/lib_xml.py`. Python Software Foundation License Version 2 (its LICENSE file; metadata `License: PSFL`) |
| [huggingface_hub](https://pypi.org/project/huggingface-hub/) | `tools/stage_model/requirements-cluster.txt` | 2.3.0 | Apache-2.0 | Model-stage Job |
| [iniconfig](https://pypi.org/project/iniconfig/) | `requirements-test.txt` | 2.3.1 | MIT | test suites (pytest) |
| [jsonschema](https://pypi.org/project/jsonschema/) | `requirements-test.txt` | 4.26.0 | MIT | test suites |
| [jsonschema-specifications](https://pypi.org/project/jsonschema-specifications/) | `requirements-test.txt` | 2025.9.1 | MIT | test suites (jsonschema) |
| [packaging](https://pypi.org/project/packaging/) | `requirements-test.txt` | 26.3 | Apache-2.0 OR BSD-2-Clause | test suites (pytest) |
| [pluggy](https://pypi.org/project/pluggy/) | `requirements-test.txt` | 1.7.0 | MIT | test suites (pytest) |
| [pygments](https://pypi.org/project/pygments/) | `requirements-test.txt` | 2.21.0 | BSD-2-Clause | test suites (pytest) |
| [pytest](https://pypi.org/project/pytest/) | `requirements-test.txt` | 9.1.1 | MIT | test suites |
| [PyYAML](https://pypi.org/project/PyYAML/) | `requirements-test.txt` | 6.0.3 | MIT | test suites |
| [referencing](https://pypi.org/project/referencing/) | `requirements-test.txt` | 0.37.0 | MIT | test suites (jsonschema) |
| [rpds-py](https://pypi.org/project/rpds-py/) | `requirements-test.txt` | 2026.9.1 | MIT | test suites (jsonschema, referencing) |
| [typing-extensions](https://pypi.org/project/typing-extensions/) | `requirements-test.txt` | 4.16.0 | PSF-2.0 | test suites (referencing, Python below 3.13) |

### Models

The weights are downloaded by the reader into their own bucket (or read from the Hub at run time) and are not
distributed here.

| Component | In this repository | Version | License | Notes |
|---|---|---|---|---|
| [Qwen/Qwen3.8-27B-FP8](https://huggingface.co/Qwen/Qwen3.8-27B-FP8) | `terraform/cluster/terraform.tfvars.example` | revision 017b9c7af6b5689d5dd426a76e0bc077eb5ca20a | Apache-2.0 | The served model of the examples; license of its model card. Staged by the model-stage Job |

## License of this repository

Everything in this repository that is not listed in the first part was written for this sample and is licensed under
the MIT-0 License: see [LICENSE](LICENSE).
