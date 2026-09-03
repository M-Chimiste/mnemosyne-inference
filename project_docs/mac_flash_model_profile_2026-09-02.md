# Mac Flash model profile — 2026-09-02

## Outcome

Athena and Metis behave like single-decoder llama.cpp servers for these model
artifacts. Two client requests are enough to keep the decoder fed; four or more
do not improve aggregate output throughput and sharply increase tail latency.
For Fleet batch jobs, use an item concurrency of 2 and split larger logical jobs
into bounded groups (8 items was exercised end to end).

The context limits currently advertised by the Macs are deployment settings,
not all model limits. Athena caps both models at 65,536 tokens. Metis now exposes
both native limits: 262,144 for Qwen3.8-Flash-Next and 1,048,576 for
GLM-5.3-Flash. Qwen's optional YaRN extension is outside this profile.

No prompt or response content was retained by the benchmark. The saved results
below are latency, status, usage, and throughput aggregates.

## Test surface

- Fleet: `https://nyx.tail6ebd6b.ts.net`
- Direct nodes: Athena and Metis native inference listeners
- Engine reported by both nodes: llama.cpp
- Workload: unique synthetic prompts, 64 generated tokens, low reasoning effort
- Resident-model measurements: four requests at concurrency 1, 2, and 4
- Batch validation: eight unique strict-JSON judge requests through Fleet

The public inference planes do not expose SoC or memory SKU, so this report does
not guess which Mac Studio hardware revision is behind either node.

## Current context contracts

| Node | Model | Policy | Detected native | Configured/effective | Verified long prefill |
|---|---|---:|---:|---:|---:|
| Athena | Qwen3.8-Flash-Next | automatic | 262,144 | 65,536 | none recorded |
| Metis | Qwen3.8-Flash-Next | native | 262,144 | 262,144 | none recorded |
| Athena | GLM-5.3-Flash | automatic | 1,048,576 | 65,536 | none recorded |
| Metis | GLM-5.3-Flash | native | 1,048,576 | 1,048,576 | none recorded |

Observed probes:

- Before Metis was changed to native mode, Qwen completed a cache-free near-64K
  prefill through Fleet in 54.3 seconds. A larger request was rejected with
  `n_ctx = 65536`, proving the then-active cap. Metis now advertises 262,144.
- Metis GLM completed a cache-free near-64K prefill in 142.2 seconds. A
  deliberately oversized request was rejected with `n_ctx = 1048576`, proving
  the configured admission ceiling, not that a full 1M prefill is practical.
- Athena GLM accepted a near-64K request but it did not finish within the
  ten-minute practical cutoff. Cancelling the client released the Fleet
  reservation; a subsequent small request succeeded.

At Metis's measured warm 64K GLM prefill rate, a full native prefill would be a
roughly 35–40 minute operation if scaling stayed linear. Treat one million as
an offline capacity tier, not an interactive request target, until a full
profile completes on that exact runtime and Mac.

Qwen's official model card reports 262,144 native tokens. Its raw checkpoint configuration declares
`max_position_embeddings = 262144`. GLM's checkpoint configuration declares
`max_position_embeddings = 1048576`.

References:

- https://huggingface.co/Qwen/Qwen3.8-Flash-Next-FP8
- https://huggingface.co/Qwen/Qwen3.8-Flash-Next-FP8/raw/main/config.json
- https://huggingface.co/zai-org/GLM-5.3-Flash/raw/main/config.json
- https://recipes.vllm.ai/Qwen/Qwen3.8-Flash-Next

## Saturation results

### Direct, resident model

| Model / node | Client concurrency | Aggregate output tok/s | p50 TTFT | p95 TTFT | p95 total |
|---|---:|---:|---:|---:|---:|
| Qwen / Athena | 1 | 24.34 | 652 ms | 655 ms | 2.73 s |
| Qwen / Athena | 2 | 26.77 | 2.54 s | 3.05 s | 5.03 s |
| Qwen / Athena | 4 | 27.04 | 3.10 s | 7.52 s | 9.47 s |
| Qwen / Metis | 1 | 23.64 | 677 ms | 683 ms | 2.80 s |
| Qwen / Metis | 2 | 29.36 | 2.09 s | 2.70 s | 4.64 s |
| Qwen / Metis | 4 | 29.06 | 2.82 s | 6.85 s | 8.81 s |
| GLM / Athena | 1 | 19.51 | 817 ms | 823 ms | 3.28 s |
| GLM / Athena | 2 | 22.52 | 2.91 s | 3.33 s | 5.98 s |
| GLM / Athena | 4 | 23.23 | 3.33 s | 8.55 s | 11.02 s |
| GLM / Metis | 1 | 19.31 | 843 ms | 898 ms | 3.32 s |
| GLM / Metis | 2 | 22.67 | 2.77 s | 3.38 s | 5.87 s |
| GLM / Metis | 4 | 22.54 | 3.60 s | 8.87 s | 11.36 s |

The useful knee is concurrency 2. Compared with concurrency 1, it improves
aggregate output by about 10–24%, depending on node/model. Concurrency 4 adds
at most about 3% over concurrency 2, and often nothing, while approximately
doubling p95 latency. Concurrency 8 and 16 through Fleet likewise did not
produce another throughput tier.

Longer 256-token generations confirmed the same behavior: Qwen delivered
29.7 output tok/s at concurrency 1 and 30.7 at concurrency 2; GLM delivered
22.8 and 23.5 respectively. These servers interleave/queue work but do not
provide vLLM-style continuous-batch scaling.

### Fleet `/v1/batches` with a judge-shaped payload

Each job contained eight unique requests and required a strict JSON object with
`score`, `related`, and `rationale` fields.

| Model | Batch item concurrency | Eight-item wall time | Result |
|---|---:|---:|---|
| Qwen | 1 | 31.38 s | 8 complete, valid schema |
| Qwen | 2 | 28.04 s | 8 complete, valid schema |
| Qwen | 4 | 27.94 s | 8 complete, valid schema |
| GLM | 1 | 88.95 s | 8 complete, valid schema |
| GLM | 2 | 85.22 s | 8 complete, valid schema |
| GLM | 4 | 85.88 s | 8 complete, valid schema |

Use 2. Four consumes queue slots without a material wall-time gain.

Reasoning controls are part of the performance contract:

- Qwen judge calls need low reasoning plus explicit thinking disablement and a
  strict response schema. Low reasoning alone exhausted a 256-token budget in
  reasoning without producing JSON in one test.
- GLM defaults to maximum reasoning. A 256-token default call produced only
  reasoning and ended at the length limit. Low reasoning, `clear_thinking`, and
  a strict schema produced a valid judge object in 104 completion tokens.

## Removing the artificial 64K caps

### GLM-5.3-Flash

Set Athena's GLM profile to **Model native maximum** (1,048,576). This causes
llama.cpp to receive `--ctx-size 1048576`, makes the native service advertise
that value as `max_model_len`, and requires no RoPE extrapolation. Metis is
already configured this way.

### Qwen3.8-Flash-Next

Set Athena's Qwen profile to **Model native maximum** (262,144). This causes
llama.cpp to receive `--ctx-size 262144` and makes the native service advertise
262,144 as `max_model_len`. Metis is already configured this way. Do not relabel
the optional one-million-token YaRN extension as native; it is deliberately out
of scope here.

The existing **Model native maximum** policy is sufficient; no schema change is
needed. It deliberately requests the checkpoint's detected native value without
claiming that a full-window prefill has been benchmarked. After changing the
profiles, record peak resident memory, macOS memory pressure, swap, load time,
prefill rate, and post-cancel health. An allocation that starts is not yet a
performance result.

### Fleet visibility

Nyx currently returns only `id`, `object`, and `owned_by` from `/v1/models`; it
does not surface `max_model_len` or the structured context contract that each
native node exposes. Once both replicas use the same native-context load
contract, they can collapse behind one exact Fleet deployment as intended.
Fleet should also expose that deployment's native `max_model_len` so
TheseusInsight can reject oversized requests before submission.

## TheseusInsight integration

Treat Nyx as one logical inference pool. Do not register Athena and Metis as
independent Theseus judge servers alongside Nyx: that would create a second
scheduler, bypass Fleet's warm/capacity policy, and encourage model-switch
thrashing on machines that keep one model resident.

TheseusInsight currently accepts only `ollama` and `lmstudio` in its shared
inference-server registry. Its judge worker creates one synchronous subprocess
worker per row, and it strips `http://` or `https://` before constructing the
LM Studio client. Consequently, representing Nyx as a fake LM Studio row loses
HTTPS and limits the whole Fleet to one in-flight task.

Recommended adapter:

1. Add a first-class `mnemosyne-fleet` (or generic OpenAI-compatible) provider
   that preserves the full HTTPS base URL and obtains any bearer from an
   environment-backed secret.
2. For scoring, submit bounded Fleet batch jobs of eight items with
   `max_concurrency = 2`, batch priority, unique `custom_id` values, strict JSON
   schema, and model-specific reasoning controls. Poll and commit each result
   independently. Never retry an ambiguous timeout.
3. Keep interactive research traffic on Fleet's normal/interactive lanes.
   Batches should be background work, not eight ordinary concurrent calls.
4. Use Qwen for routine judging/summarization where its measured latency is
   materially better. Use GLM when its answer quality or long native context is
   needed. Keep ordinary summary chunks around 8K–32K; a nominal 1M admission
   limit is not a sensible default chunk size on these Macs.
5. Discover context tiers from Fleet metadata or static, config-owned aliases.
   Reject locally before submission when prompt plus output budget exceeds the
   selected tier.

This integration can be delivered incrementally: first an HTTPS-preserving
OpenAI-compatible provider with two in-flight calls, then native Fleet batch-job
submission for judge workloads. The second phase provides the cleaner priority
and backpressure behavior.

## Reproduction

The benchmark client supports `--unique-prompts` so whole-prompt caching cannot
inflate prefill or saturation measurements:

```bash
uv run --project macos/service python macos/scripts/benchmark_native.py \
  --base-url https://nyx.tail6ebd6b.ts.net \
  --model qwen3.8-flash-next \
  --requests 8 --concurrency 2 --max-tokens 64 \
  --priority batch --reasoning-effort low --unique-prompts
```

Run the context profiler from the Mac Settings UI so it can acquire the global
empty maintenance barrier and persist evidence for that exact model, runtime,
OS, and Mac. Client-side oversize probes identify an admission ceiling, but do
not replace the service-owned context profile.
