# Logos and icons in the architecture diagram

[architecture.drawio](architecture.drawio) (draw.io source) and [architecture.png](architecture.png) (its export) are
shipped as drawn. They show AWS Architecture Icons, one Kubernetes icon and seven embedded logo images of three open
source projects. Grafana is shown as a text label only, without its logo.

Logos are trademarks of their owners; the project licenses cover the software/artwork as stated upstream and do not grant any endorsement.

## Embedded logos

The seven `shape=image` cells of `architecture.drawio` embed three SVG files, unmodified. Each embedded SVG is
byte-identical (sha256 below) to the upstream file on the project's default branch as fetched on 2026-10-26.

| Cells in `architecture.drawio` | Product | Upstream file | License as published upstream |
|---|---|---|---|
| `operator-icon`, `s0-frontend-logo`, `s7-frontend-logo` | NVIDIA Dynamo | [ai-dynamo/dynamo: `docs/fern/assets/img/dynamo-logo.svg`](https://github.com/ai-dynamo/dynamo/blob/main/docs/fern/assets/img/dynamo-logo.svg) | Apache License 2.0, the repository [LICENSE](https://github.com/ai-dynamo/dynamo/blob/main/LICENSE) |
| `s0-workers-sglang`, `s7-prefill-sglang`, `s7-decode-sglang` | SGLang | [sgl-project/sglang: `assets/logo_square.svg`](https://github.com/sgl-project/sglang/blob/main/assets/logo_square.svg) | Apache License 2.0, the repository [LICENSE](https://github.com/sgl-project/sglang/blob/main/LICENSE) |
| `prometheus-logo` | Prometheus | [cncf/artwork: `projects/prometheus/icon/color/prometheus-icon-color.svg`](https://github.com/cncf/artwork/blob/main/projects/prometheus/icon/color/prometheus-icon-color.svg) | No software license: the repository [LICENSE.md](https://github.com/cncf/artwork/blob/main/LICENSE.md) makes all artwork available under the [Linux Foundation trademark usage guidelines](https://www.linuxfoundation.org/trademark-usage/) |

| Product | sha256 of the embedded SVG |
|---|---|
| NVIDIA Dynamo | `4019188fbddfe63ae7a9ac420a19191e951921157d63b773d2ae3b61ff03728e` |
| SGLang | `09ccea391ea4da9d896d6fae0f31e5a52a456bfe837f266a99a69c2242d95fe3` |
| Prometheus | `a96d48cc7f1c91de940dacbf30581850fd2c0ed4afaf1b0b94fb86eb1d0aef6e` |

## Other icons

- **AWS Architecture Icons** (the `mxgraph.aws4` shapes: AWS Cloud, Region, VPC, EC2 instance contents, Amazon EKS,
  Amazon S3, Amazon ECR, Amazon CloudFront, Network Load Balancer, Elastic Fabric Adapter, Users). Terms at
  [AWS Architecture Icons](https://aws.amazon.com/architecture/icons/): "We allow customers and partners to use these
  toolkits and assets to create architecture diagrams."
- **Kubernetes Job icon** (cell `driver-icon`, `mxgraph.kubernetes.icon2` with `prIcon=job`): drawn by the draw.io
  Kubernetes shape library, not embedded. The
  [Kubernetes Icons Set](https://github.com/kubernetes/community/tree/master/icons) "is licensed under a choice of either
  Apache-2.0 or CC-BY-4.0"; that the draw.io shape is derived from that set is not verified.
- **draw.io shape libraries** (both bullets above are drawn by them, none is embedded): the
  [jgraph/drawio README](https://github.com/jgraph/drawio#license) licenses the source code under the Apache License 2.0
  and gives the icon sets and stencil libraries their own terms, which state: "This restriction does not apply to
  end-user diagram output (such as exported images or documents) created using this software."
