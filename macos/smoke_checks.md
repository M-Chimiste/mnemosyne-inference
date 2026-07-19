# Native macOS smoke checks

Run these checks on the target Apple Silicon workstation. Automated tests use
fake engines and cannot validate Metal memory release, upstream API drift,
LaunchAgent behavior, or model quality.

## 1. Configuration and listeners

```bash
uv run --project macos/service mnemosyne-macos --check-config \
  --config "$HOME/Library/Application Support/Mnemosyne/config.yaml" \
  --env "$HOME/Library/Application Support/Mnemosyne/.env"
lsof -nP -iTCP:17320 -iTCP:17321 -iTCP:17322 -iTCP:17323 -sTCP:LISTEN
```

Confirm Mnemosyne owns `17320`/`17321`, oMLX owns `17322`, DS4 is absent while
unloaded, and every listener is loopback-only. LM Studio should own `1234`.

## 2. Clean startup and LM Studio

Start Mnemosyne with every configured engine reporting no resident model.

```bash
curl -s http://127.0.0.1:17321/manager/status | jq
curl -s http://127.0.0.1:17320/v1/models | jq
curl -s http://127.0.0.1:17320/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local-qwen","messages":[{"role":"user","content":"Reply with LM Studio."}]}' | jq
```

Verify LM Studio reports exactly one loaded instance and the public response
uses the configured alias. Unload through `POST :17321/manager/unload` and
verify the native LM Studio model list is empty.

## 3. oMLX lifecycle

Request the configured oMLX alias. Confirm Mnemosyne unloads LM Studio first,
oMLX reports exactly one loaded pool model, and a second request reuses it.
Exercise non-streaming and streaming chat, Responses, embeddings, or rerank as
allowed by that profile. Explicit unload must converge without an admin-auth
error.

If oMLX rejects unload, do not weaken strict residency. Keep it loopback-only
and correct its admin authentication/session configuration.

## 4. DS4 lifecycle

Request the DS4 alias and confirm:

- `ds4-server` starts with the configured GGUF, context, `127.0.0.1:17323`, and
  KV-cache arguments;
- `/v1/models` becomes ready before the client request is proxied;
- OpenAI Chat/Completions, Responses, and Anthropic Messages behave as expected;
- unloading sends TERM to the owned process group, escalates only after the
  grace period, and leaves GGUF/KV files intact;
- an unrelated process occupying `17323` is reported and never signaled.

## 5. Cross-engine drain and strict residency

Begin a long streaming request on each engine. While it is active, request an
alias on another engine. The old stream must finish (or disconnect) before its
engine unloads. Continuous new traffic for the old target must not starve the
queued switch. Sample Activity Monitor or `ps` throughout and confirm two model
engines are never resident together.

Directly load a second model through LM Studio or oMLX, then call:

```bash
curl -X POST http://127.0.0.1:17321/manager/reconcile | jq
```

Mnemosyne must detect the drift, unload it, and never load another target while
any enabled adapter has uncertain state.

## 6. Usage outbox

With the Postgres DSN intentionally unreachable, complete streaming and
non-streaming requests through each engine. Confirm local rows and a growing
outbox through `GET /manager/usage`. Restore Postgres, wait one flush interval,
and verify the outbox drains once with no duplicate `event_id` rows centrally.

## 7. LaunchAgent and menu app

Stage and sign the app, move it to `/Applications`, enable **Background
service**, and approve it in Login Items if requested. Confirm:

- the service survives **Quit Menu App**;
- the menu app can list/load/unload configured aliases after reopening;
- disabling the background service unregisters the LaunchAgent;
- an unexpected service exit is restarted by `KeepAlive`;
- login starts the service without a terminal or Docker Desktop;
- logs and private config live below
  `~/Library/Application Support/Mnemosyne/`.
