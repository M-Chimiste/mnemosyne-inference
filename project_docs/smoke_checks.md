# Manual Smoke Checks — Pre-Refactor Baseline

**Purpose:** captures behavior of the routes that need a live container + GPU,
which the pytest harness in [../tests/test_smoke.py](../tests/test_smoke.py) cannot exercise.
Together with the pytest contract snapshot, this is the canonical record of
"what worked before the refactor". Phase 1+ regressions show up here.

**When to run:** before merging any phase that touches request paths
(`_proxy`, `_maybe_swap`, `_start_vllm`, `_kill_vllm`, `_run_download`).

**Setup:**
```bash
vllm-ctl build && vllm-ctl start
# wait for /health → 200
export MGR=http://localhost:8000
```

A small model is enough for every check below — `Qwen/Qwen2.5-7B-Instruct`
is a good default. Adjust `tp` to match the box.

---

## 1. Cold load

```bash
curl -sX POST $MGR/manager/load \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct","tp":1,"gpu_mem":0.85}'
```

**Expect:** 200 within `VLLM_STARTUP_TIMEOUT` (default 600s). Then:

```bash
curl -s $MGR/manager/status | jq
```

`loaded_model` is the model id, `vllm_pid` is non-null, `loaded_at` is a
unix timestamp, `loaded_at_human` is a human string, `loading` is `false`.

---

## 2. Direct proxy (non-streaming)

```bash
curl -sX POST $MGR/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"Qwen/Qwen2.5-7B-Instruct",
    "messages":[{"role":"user","content":"reply with the word OK"}],
    "max_tokens":5
  }' | jq '.choices[0].message.content'
```

**Expect:** a JSON completion with the assistant message. No swap occurs
(the `model` matches the loaded one).

---

## 3. Streaming proxy

```bash
curl -NsX POST $MGR/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"Qwen/Qwen2.5-7B-Instruct",
    "messages":[{"role":"user","content":"count to three"}],
    "stream":true,
    "max_tokens":20
  }'
```

**Expect:** SSE frames stream incrementally (`data: {...}` lines), terminated
by `data: [DONE]`. No buffering — chunks should arrive as the model
generates them, not all at once at the end.

---

## 4. Auto-swap on `/v1/*`

With `Qwen/Qwen2.5-7B-Instruct` loaded, request a different model:

```bash
curl -sX POST $MGR/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"Qwen/Qwen2.5-Coder-1.5B-Instruct",
    "messages":[{"role":"user","content":"hi"}],
    "max_tokens":5
  }'
```

**Expect:** the request blocks while the swap happens (unload + load), then
returns a completion. After the call:

```bash
curl -s $MGR/manager/status | jq .loaded_model
# → "Qwen/Qwen2.5-Coder-1.5B-Instruct"
```

---

## 5. Bad model id

```bash
curl -sX POST $MGR/manager/load \
  -H 'Content-Type: application/json' \
  -d '{"model":"nonsense/does-not-exist"}'
```

**Expect:** non-2xx response with a clear error message. Critically:

```bash
curl -s $MGR/manager/status | jq '.loaded_model, .vllm_pid'
```

shows `null, null` — no zombie subprocess survives the failed load.

---

## 6. Download lifecycle

```bash
curl -sX POST $MGR/manager/download \
  -H 'Content-Type: application/json' \
  -d '{"model":"hf-internal-testing/tiny-random-gpt2"}'
```

**Expect:** 200 with:

```json
{
  "status": "started",
  "model": "hf-internal-testing/tiny-random-gpt2",
  "poll": "/manager/download/hf-internal-testing%2Ftiny-random-gpt2"
}
```

Poll:

```bash
curl -s $MGR/manager/download/hf-internal-testing/tiny-random-gpt2 | jq .status
```

Because the download thread starts immediately, the first observed status may
already be any valid state: `queued`, `downloading`, `complete`, or `error`.
For this valid tiny model, repeated polling should eventually reach `complete`.
List view:

```bash
curl -s $MGR/manager/downloads | jq
# → {"downloads": [{...}]}
```

---

## 7. Unload

```bash
curl -sX POST $MGR/manager/unload
curl -s $MGR/manager/status | jq
```

**Expect:** `loaded_model: null`, `vllm_pid: null`, `loaded_at: null`.
([vllm_manager.py:173-185](../vllm_manager.py).)

---

## Recording deltas

If any step above behaves differently from the description on the current
`main`, that's a pre-existing bug — open it as a separate issue rather than
fixing in-flight. Phase 0's contract snapshot must stay faithful to what
the code does today.
