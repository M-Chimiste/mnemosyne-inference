# Manual Workstation Smoke Checks

**Purpose:** captures behavior of routes that need a live container + GPU,
which the pytest harness cannot exercise. Together with the pytest contract
snapshot, this is the workstation-side release checklist.

**When to run:** before marking Phase 8 / v1 acceptance complete, and before
merging any future phase that touches request paths, process lifecycle, install
workflows, or plane separation.

**Setup:**

```bash
vllm-ctl build && vllm-ctl start

# Current two-plane endpoints.
export INF=http://localhost:8000
export ADMIN=http://localhost:8001

# Admin password is configured in the compose dir .env and required for
# /manager/*, /docs, and /ui/* on the admin plane.
export ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' ~/vllm-manager/.env | head -1 | cut -d= -f2-)"

# wait for /health -> 200
curl -fsS "$INF/health"
```

A small model is enough for every text-generation check below:
`Qwen/Qwen2.5-7B-Instruct` is a good default. Adjust `tp` to match the box.

---

## 1. Cold load

```bash
curl -sX POST "$ADMIN/manager/load" \
  -u admin:"$ADMIN_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct","tp":1,"gpu_mem":0.85}'
```

**Expect:** 200 within `VLLM_STARTUP_TIMEOUT` (default 600s). Then:

```bash
curl -s "$ADMIN/manager/status" -u admin:"$ADMIN_PASSWORD" | jq
```

`loaded_model` is the model id, `vllm_pid` is non-null, `loaded_at` is a
unix timestamp, `loaded_at_human` is a human string, and `loading` is `false`.

---

## 2. Direct proxy (non-streaming)

```bash
curl -sX POST "$INF/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"Qwen/Qwen2.5-7B-Instruct",
    "messages":[{"role":"user","content":"reply with the word OK"}],
    "max_tokens":5
  }' | jq '.choices[0].message.content'
```

**Expect:** a JSON completion with the assistant message. No swap occurs
because the `model` matches the loaded one.

---

## 3. Streaming proxy

```bash
curl -NsX POST "$INF/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"Qwen/Qwen2.5-7B-Instruct",
    "messages":[{"role":"user","content":"count to three"}],
    "stream":true,
    "max_tokens":20
  }'
```

**Expect:** SSE frames stream incrementally (`data: {...}` lines), terminated
by `data: [DONE]`. Chunks should arrive as the model generates them, not all
at once at the end.

---

## 4. Auto-swap on `/v1/*`

With `Qwen/Qwen2.5-7B-Instruct` loaded, request a different model:

```bash
curl -sX POST "$INF/v1/chat/completions" \
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
curl -s "$ADMIN/manager/status" -u admin:"$ADMIN_PASSWORD" | jq .loaded_model
# -> "Qwen/Qwen2.5-Coder-1.5B-Instruct"
```

---

## 5. Bad model id

```bash
curl -sX POST "$ADMIN/manager/load" \
  -u admin:"$ADMIN_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"model":"nonsense/does-not-exist"}'
```

**Expect:** non-2xx response with a clear error message. Critically:

```bash
curl -s "$ADMIN/manager/status" -u admin:"$ADMIN_PASSWORD" | jq '.loaded_model, .vllm_pid'
```

shows `null, null` — no zombie subprocess survives the failed load.

---

## 6. Download lifecycle

```bash
curl -sX POST "$ADMIN/manager/download" \
  -u admin:"$ADMIN_PASSWORD" \
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
curl -s "$ADMIN/manager/download/hf-internal-testing/tiny-random-gpt2" \
  -u admin:"$ADMIN_PASSWORD" | jq .status
```

Because the download subprocess starts immediately, the first observed status
may already be any valid state: `queued`, `downloading`, `complete`, or
`error`. For this valid tiny model, repeated polling should eventually reach
`complete`. List view:

```bash
curl -s "$ADMIN/manager/downloads" -u admin:"$ADMIN_PASSWORD" | jq
# -> {"downloads": [{...}]}
```

---

## 7. Plane separation

Admin operations must be unavailable on the inference plane and reachable on
the admin plane with Basic auth.

```bash
# Inference plane: no /manager routes.
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST "$INF/manager/load" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct"}'
# -> 404

# Admin plane: same route exists behind Basic auth.
curl -s -o /dev/null -w '%{http_code}\n' \
  "$ADMIN/manager/status" \
  -u admin:"$ADMIN_PASSWORD"
# -> 200

curl -s "$ADMIN/manager/status" -u admin:"$ADMIN_PASSWORD" | jq
```

**Expect:** inference returns 404; admin returns a real admin-plane response
when authenticated.

---

## 8. Unload

```bash
curl -sX POST "$ADMIN/manager/unload" -u admin:"$ADMIN_PASSWORD"
curl -s "$ADMIN/manager/status" -u admin:"$ADMIN_PASSWORD" | jq
```

**Expect:** `loaded_model: null`, `vllm_pid: null`, `loaded_at: null`.
([vllm_manager.py:173-185](../vllm_manager.py).)

---

## 9. Vision-model multimodal passthrough (Phase 8)

The pytest suite proves `_proxy` does not mutate the body when an
`image_url` content block is present, but only a real vision model can prove
the bytes reach the kernel and produce a sensible answer. Run on a CUDA host
with at least one Blackwell-class GPU.

```bash
# 1. Install a small vision model (about 8 GB, fits a 24 GB card).
vllm-ctl install qwen-vl-7b Qwen/Qwen2.5-VL-7B-Instruct \
  --storage nvme-fast \
  -- --max-model-len 32768

# Watch the install complete:
vllm-ctl install-status qwen-vl-7b

# 2. POST an OpenAI-format chat request with an image block.
curl -sX POST "$INF/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"qwen-vl-7b",
    "messages":[{
      "role":"user",
      "content":[
        {"type":"text","text":"Describe this image in one sentence."},
        {"type":"image_url","image_url":{
          "url":"https://upload.wikimedia.org/wikipedia/commons/thumb/4/4d/Cat_November_2010-1a.jpg/640px-Cat_November_2010-1a.jpg"
        }}
      ]
    }],
    "max_tokens":64
  }' | jq '.choices[0].message.content'
```

**Expect:** the response contains a coherent caption referring to a cat.
Failure modes worth distinguishing:

- `404` — the alias did not install; check `install-status`.
- `200` with irrelevant text — the multimodal preprocessor failed upstream;
  check `vllm-ctl logs` for vLLM errors.
- The first call after a fresh install pays the lazy-load latency. A second
  call should be quick.

This smoke check satisfies the manual side of PRD §7's "vision model
end-to-end" criterion. Phase 8's
[phase_8_acceptance.md](phase_8_acceptance.md) records pass/fail.

## Recording deltas

If any step above behaves differently from the description on the current
branch, record it in [phase_8_acceptance.md](phase_8_acceptance.md) or open a
follow-up issue before marking v1 accepted.
