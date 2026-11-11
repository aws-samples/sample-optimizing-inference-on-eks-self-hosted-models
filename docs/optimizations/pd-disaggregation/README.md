# D: Prefill/decode disaggregation

Prefill/decode (PD) disaggregation runs prompt processing (prefill) and token generation (decode) in separate workers on
separate GPUs, and copies each request's KV cache from the prefill GPU to the decode GPU. This page covers how Dynamo and
SGLang do it, what the PD examples set, how to check that the KV moved over EFA, and what we saw without `privileged` pods.

[Repo README](../../../README.md) · [R: KV router](../kv-router/README.md) · [O: HiCache](../hicache/README.md) · [examples](../../../examples/README.md)

Dynamo links open tag `v1.6.1` of ai-dynamo/dynamo, the `DYNAMO_RUNTIME_VERSION` of [`versions.env`](../../../versions.env).
For a local copy: `git clone --depth 1 --branch v1.6.1 https://github.com/ai-dynamo/dynamo.git`.

## How it works

Dynamo calls decode the memory-bound phase and prefill the computation-bound one, and says that dedicated prefill engines
let ongoing decodes proceed without being blocked by long prefills ([concepts, line 7][c7]). A request takes these steps:

1. The Frontend activates an internal prefill router when it discovers prefill and decode workers registered for the same
   model ([router guide, lines 8-18][r8]).
2. The prefill router picks a prefill worker by KV overlap and load, or by load balancing ([concepts, line 46][c46]). A
   prefill worker that declares no router config inherits the decode router mode ([activation.rs, lines 60-71][act]). With
   `DYN_ROUTER_MODE=round-robin`, the Frontend of `03-pd-disaggregation` logged
   `prefill_router_mode=RoundRobin decode_router_mode=RoundRobin advertised=false` (observed by us). So `DYN_ROUTER_MODE`
   on the Frontend decides this choice: `round-robin` in `03-pd-disaggregation` and `06-pd-hicache`, `kv` in
   `05-kv-router-pd` and `07-all-three` ([KV router guide](../kv-router/README.md)).
3. The SGLang prefill worker returns `bootstrap_info` (host, port, room) as its first output ([prefill handler, lines
   170-174][ph1] and [271-278][ph2]); the decode worker refuses a request without it ([decode handler, lines 734-740][dh]).
4. Decode routing turns off overlap scoring and prefill-token tracking, and the KV-reuse assumption unless the backend
   can deduplicate transferred blocks ([router guide, lines 26-29][r26]).
5. NIXL copies the KV cache from prefill VRAM to decode VRAM without blocking forward passes ([concepts, line 18][c18]);
   with SGLang, decode can begin while the transfer proceeds ([concepts, line 58][c58]).
6. Once a request is sent to prefill, a prefill or handoff failure is returned to it; it is not retried as aggregated
   ([router guide, line 23][r23]).

NIXL is the transfer API; the backend under it is a separate choice. Dynamo's EFA guide says that the LIBFABRIC backend
must be selected for EFA, that SGLang uses UCX, which falls back to TCP, when `SGLANG_DISAGGREGATION_NIXL_BACKEND` is
unset, and that a wrong choice shows as a high TTFT, not as an error ([efa-on-aws.mdx, lines 144-157][e144]).

## When it helps and what it costs

From Dynamo's guidance ([Kubernetes guide, lines 68-71][k68]; [concepts, lines 117-126][c117]):

- It can help when prefill and decode contend under sustained load: separate pools can isolate decode latency from long
  prompts.
- It is not automatically better: for small models, short prompts, low concurrency or clusters without a fast
  KV-transfer fabric, Dynamo calls an aggregated deployment simpler and often faster.
- The prefill GPUs' KV cache is not used during decode; in the examples 4 of the 8 GPUs prefill. When decode KV capacity
  is the bottleneck, more prefill replicas can reduce total serving capacity, unless the latency benefit compensates for
  the GPUs removed from decode.

## What the examples set

`examples/03-pd-disaggregation` replaces the aggregated `Worker` of the baseline with a `prefill` and a `decode`
component, 4 replicas each, one GPU and one EFA device (`vpc.amazonaws.com/efa`) per pod: 8 GPUs.
The command of [examples/README.md](../../../examples/README.md) on `03-pd-disaggregation` shows every change; the PD
ones are:

- flags `--disaggregation-mode prefill|decode`, `--disaggregation-transfer-backend nixl` and
  `--disaggregation-bootstrap-port 12345`;
- env `SGLANG_DISAGGREGATION_NIXL_BACKEND=LIBFABRIC`, plus `FI_PROVIDER=efa`, `FI_EFA_USE_DEVICE_RDMA=1`,
  `FI_EFA_ENABLE_SHM_TRANSFER=0` and `FI_EFA_ENABLE_SHM=0`, which Dynamo's EFA guide lists as required, and the EFA
  library directory first in `LD_LIBRARY_PATH` (Dynamo's EFA guide lists a longer value) ([efa-on-aws.mdx, lines
  159-172][e159]);
- no `IPC_LOCK` and `SYS_RESOURCE` capabilities, which the baseline `Worker` adds ([Privileged pods](#privileged-pods)).

The baseline already sets `sharedMemorySize: 16Gi`, which PD needs: NIXL stages transfer metadata in `/dev/shm` and fails
to initialize with the pod default ([Kubernetes guide, line 217][k217]).

Dynamo warns that prefill and decode with a different model, dtype, block size or KV layout give transfer errors or
corrupt output ([Kubernetes guide, lines 197-201][k197]). In the examples the decode component differs from the prefill
one only in its name, `--disaggregation-mode`, its memory limit and its `type`. The pod settings, security context
included, are listed in [the example README](../../../examples/03-pd-disaggregation/README.md). `05-kv-router-pd` changes
the router mode; `06-pd-hicache` and `07-all-three` add HiCache on the prefill workers only
([HiCache guide](../hicache/README.md)).

## Check that it works

- Every prefill and decode engine logs `Backend LIBFABRIC was instantiated` and
  `NIXL KVManager initialized with backend: LIBFABRIC` (the command in
  [the example README](../../../examples/03-pd-disaggregation/README.md) reads the prefill pods; repeat it with
  `nvidia.com/dynamo-component-type=decode`). `Backend UCX was instantiated` means the backend
  setting did not apply ([efa-on-aws.mdx, lines 253-259][e253]). We saw both LIBFABRIC lines in the two engines of a
  one-prefill, one-decode run on the image of `versions.env`.
- The prefill workers export `sglang:kv_transfer_total_mb` on their `system` port (9090 in the manifest). In the same run
  its `_count` grew by one for one request:

  ```bash
  kubectl -n s-dis exec <prefill-pod> -c main -- sh -c 'curl -s localhost:9090/metrics | grep "^sglang:kv_transfer_total_mb_count"'
  ```

- On the TCP path the KV would cross the pod's network interface, which `/proc/net/dev` in the container counts
  ([proc_pid_net(5)][procnet]). Read `kubectl -n s-dis exec <prefill-pod> -c main -- cat /proc/net/dev` before and after
  a request with a prompt of a few thousand tokens: bytes that grow by about the KV size mean the KV went over TCP. This
  is our reading of the mechanism, not tested here. See also
  [TROUBLESHOOTING](../../TROUBLESHOOTING.md#kv-transfer-silently-on-tcp-how-to-detect-and-what-changes-it).

## Privileged pods

The prefill and decode containers of the PD examples are not privileged and add no capabilities; they run as root
(`runAsUser: 0`). Dynamo's EFA guide, Step 5, says the opposite: "Dynamo pods that use EFA must request the resource and
run privileged" and "`privileged: true` is required for NIXL to register CUDA VRAM with the EFA NIC via `fi_mr_reg`.
`IPC_LOCK` alone is insufficient." ([efa-on-aws.mdx, lines 188-212][e188]). The examples ship without them because of
what we observed, within the scope below. The manifests as shipped in this repository are not the ones we applied for
that observation: we edited earlier versions by hand, as described below. Run the checks of
[Check that it works](#check-that-it-works) on your nodes.

Observed by us on 2026-11-04, not published (one node, a few requests and one 5-minute load run): on one `p5en.48xlarge`
node of EKS Auto Mode (kernel 6.12.113) with the runtime image of `versions.env`, one prefill and one decode worker of
`03-pd-disaggregation` moved the KV over NIXL LIBFABRIC on EFA without `privileged: true` and without added capabilities.
To run them we set `replicas: 1`, changed the node pool name, removed `privileged` and `capabilities` from the prefill
and decode containers, and bypassed the GPU-pin preamble that the launch script had then (the examples no longer have
it): without `privileged` the container saw only its allocated GPU, as GPU 0, and the preamble exited. In those pods:

- the effective capability set was `00000000a80425fb`, in which bit 14 (`CAP_IPC_LOCK`, [capability.h][cap]) is clear;
- `ulimit -l` printed `unlimited` in a `kubectl exec` shell;
- `/dev/infiniband` held one `uverbs` device, the one allocated to the pod;
- both engines logged `Backend LIBFABRIC was instantiated`, and their startup logs had no `fi_mr_reg` line;
- for one long-prompt request, the hardware counters of the sending and the receiving EFA device grew by about the size
  of the KV that SGLang reported moving (`sglang:kv_transfer_total_mb`). We read the counters over RDMA netlink with a
  script that is not in this repo.

The first three items come from these commands, run in the prefill and in the decode pod:

```bash
kubectl -n s-dis exec <pod> -c main -- grep CapEff /proc/self/status
kubectl -n s-dis exec <pod> -c main -- sh -c 'ulimit -l'
kubectl -n s-dis exec <pod> -c main -- ls /dev/infiniband
```

On the same node, the 4 prefill and 4 decode workers of `07-all-three`, with the same changes except the replica count
and with `--router-session-affinity-ttl-secs 600` added to the Frontend, ran a series of load steps without a pod
restart. The deployment saturated at the highest steps, and in the longest step two prefill pods answered a few health
probes with 503, each time for seconds (observed by us, one deployment).

Not tested: the `p6-b200.48xlarge` node that the examples select ([repo README](../../../README.md)) and other instance
types, the `05-kv-router-pd` and `06-pd-hicache` manifests, KV transfer between two nodes, other NIXL backends or
libfabric providers, kernels, images or Dynamo versions, non-root containers, more than one EFA device per pod, and
`hostIPC`, which Step 5 marks "required by some EFA setups". Whether NIXL registered GPU memory directly or staged it
through host memory was not checked. Within the scope above our observation contradicts Step 5; Dynamo has not
confirmed it.

Without `privileged` the examples still need a namespace at the Pod Security `privileged` level: they mount `hostPath`
volumes, which the `baseline` level forbids ([Pod Security Standards][pss]). Terraform creates the PD namespaces at
`privileged` (`terraform/cluster/namespaces.tf`).

## Not tested here

- Placement across nodes: nothing in the manifests keeps prefill and decode on one node, the only placement rule is the
  GPU node pool selector.
- Other prefill:decode splits than 4:4.
- The tuning variables of [efa-on-aws.mdx, lines 174-184][e174], which the examples do not set.

[c7]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md?plain=1#L7
[c18]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md?plain=1#L18
[c46]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md?plain=1#L46
[c58]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md?plain=1#L58
[c117]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md?plain=1#L117-L126
[r8]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/disaggregated-serving.md?plain=1#L8-L18
[r23]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/disaggregated-serving.md?plain=1#L23
[r26]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/disaggregated-serving.md?plain=1#L26-L29
[act]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/lib/llm/src/kv_router/prefill_router/activation.rs#L60-L71
[ph1]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/components/src/dynamo/sglang/request_handlers/llm/prefill_handler.py#L170-L174
[ph2]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/components/src/dynamo/sglang/request_handlers/llm/prefill_handler.py#L271-L278
[dh]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/components/src/dynamo/sglang/request_handlers/llm/decode_handler.py#L734-L740
[k68]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/kubernetes/disaggregated-serving/overview.md?plain=1#L68-L71
[k197]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/kubernetes/disaggregated-serving/overview.md?plain=1#L197-L201
[k217]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/kubernetes/disaggregated-serving/overview.md?plain=1#L217
[e144]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/kubernetes/installation/rdma-setup/efa-on-aws.mdx?plain=1#L144-L157
[e159]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/kubernetes/installation/rdma-setup/efa-on-aws.mdx?plain=1#L159-L172
[e174]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/kubernetes/installation/rdma-setup/efa-on-aws.mdx?plain=1#L174-L184
[e188]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/kubernetes/installation/rdma-setup/efa-on-aws.mdx?plain=1#L188-L212
[e253]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/kubernetes/installation/rdma-setup/efa-on-aws.mdx?plain=1#L253-L259
[cap]: https://github.com/torvalds/linux/blob/v6.12/include/uapi/linux/capability.h
[procnet]: https://man7.org/linux/man-pages/man5/proc_pid_net.5.html
[pss]: https://kubernetes.io/docs/concepts/security/pod-security-standards/#baseline

---

License: MIT-0, see [LICENSE](../../../LICENSE).
