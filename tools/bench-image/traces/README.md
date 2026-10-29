# Traces

`64k_400_90kv_agent_new_noschedule_short_15perc.jsonl` is the matrix trace. The bench image bakes it read-only as `/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl`, with `/traces/SHA256SUMS`.

## Provenance

| | |
|---|---|
| Upstream | [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo), `recipes/kimi-k2.6/perf/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl` (a Git LFS object) |
| Commit | `66a95d5aa3b5d238fd94b6f8243c4e479cb7242c` ("feat(recipes): Add K2.6 recipes (#10187)", 2026-06-03), which added the file. It is unchanged in tag `v1.5.1` (commit `2093662a4d4e5fd318732935213dde08dc7be818`). |
| Upstream file | 3541 rows, sha256 `f20d3f2bc83dd1306cda659fbe34e7c4d85ca5497626c98bc0b1c4d2211379d0` |
| License | Apache License 2.0, Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. The MIT carve-out in the upstream `LICENSE` covers only `lib/llm/tests/data/deepseek-v3.2` and does not apply to this file. |
| Changes (2026-10-18) | Removed the 48 rows with `input_length + output_length >= 500000`, the 500,000-token `context-length` of the upstream B200 recipes that reference this trace by symlink. The other 3493 rows are byte-identical to upstream and in upstream order. No timestamps were added: `demo-bench trace schedule` stamps the fixed schedule at run time. |
| This file | 3493 rows, sha256 `ddc75ce76df4eccd5e3290f4665e993dbe891f60e84942410e2485cb5425d62e` |

To reproduce it from a Dynamo checkout with Git LFS objects fetched:

```bash
jq -c 'select(.input_length + .output_length < 500000)' \
  recipes/kimi-k2.6/perf/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl \
  | shasum -a 256   # ddc75ce76df4eccd5e3290f4665e993dbe891f60e84942410e2485cb5425d62e
```

The repository [`NOTICE`](../../../NOTICE) lists this file. If you change it, add the change and the new sha256 here and in the provenance block of [`../Dockerfile`](../Dockerfile).
