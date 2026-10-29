# Matrix driver: image pins

Deployment-specific inputs of the matrix driver (`tools/matrix_driver`). This directory holds only the image pins; the driver, the plans and the launch sequence are documented in `tools/matrix_driver/README.md`.

## `images.env`

Plain `KEY=VALUE` (same format as the platform config env file of the Track Q region: no quotes, spaces or `$`). It pins two images by tag and digest, without the registry part:

| Key | Image |
|---|---|
| `MATRIX_BENCH_TAG`, `MATRIX_BENCH_DIGEST` | The bench image that runs the driver and the watchdog. |
| `MATRIX_FFMPEG_TAG`, `MATRIX_FFMPEG_DIGEST` | The ffmpeg image of the compose render step (`linuxserver/ffmpeg`, mirrored into the bench repository with the index digest preserved). |

`tools/matrix_driver/render_driver.py` joins each pin to `BENCH_REPO` from the platform config env file of the Track Q region, giving `<BENCH_REPO>:<TAG>@<DIGEST>`. Registry host, account and region are therefore never written here. To change an image, edit this file and re-render; `latest` is not accepted by the repo test.

## What else the driver reads

- The eight cell DGDs are not copied here: the driver ships `manifests/track-q-qwen38/rendered/S0.yaml` to `S7.yaml`, with one added annotation.
- The results bucket, region, bench repository and the Track Q image (used by the L3 cleanup pod) come from the platform config env file of the Track Q region (variables `RESULTS_BUCKET`, `BENCH_REPO` and the Track Q image repository variable).
- The plans are in `plans/w3-16k/` (W3-16K) and `plans/w2-long-context/` (`plan-e3` to `plan-e7`) and carry `${RESULTS_BUCKET}` placeholders.
- The Secret `bench/demo-grafana-auth` must already exist for plans with `take` steps; the driver never reads its value.

To use another deployment, change the platform config env file (or pass another file with `render_driver.py --env`) and this file, not the driver.
