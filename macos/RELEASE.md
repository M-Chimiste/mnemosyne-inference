# macOS release and recovery

`macos/VERSION` is the native product version. The app bundle, coordinator
package, isolated image worker, both lock files, release tag, DMG name, and
Sparkle appcast must agree. Run:

```bash
python3 macos/packaging/verify_release.py
python3 macos/packaging/verify_release.py --tag v0.9.0
```

Every staged candidate can also produce a private, secret-redacted acceptance
report:

```bash
python3 macos/packaging/collect_acceptance.py \
  --app "macos/app/build/Stage/Unified Inference.app" \
  --dmg "macos/app/build/Distribution/Unified-Inference-0.9.0-macos-arm64.dmg" \
  --output "macos/app/build/Distribution/Unified-Inference-0.9.0-macos-arm64-acceptance.json"
```

`build_dmg.sh` runs this automatically. The report verifies the app version,
architecture, complete embedded runtime, code seal, helper identity, absence
of Python bytecode, V1 engine defaults, DMG hash, and disk-image integrity.
It never includes a Sparkle key, authorization header, Postgres DSN, password,
HF token, bookmark, or arbitrary usage request body.

After installing the exact candidate, capture live LaunchAgent, public/control
listener, readiness, catalog, and usage evidence. Set the admin password only
through the named environment variable when control authentication is enabled:

```bash
python3 macos/packaging/collect_acceptance.py \
  --live \
  --require-live \
  --self-test your-model-alias \
  --require-guided-setup \
  --require-postgres-drain \
  --output "$HOME/Desktop/unified-inference-live-acceptance.json"
```

The optional self-test is accepted only when inference succeeds, authoritative
token usage is returned, and the matching durable local usage row is found.
For the clean-install gate, `--require-guided-setup` additionally requires the
exact app version/build to have auto-presented Setup & Health before that
self-test completed it. Reset the app preferences domain before the candidate
launch as documented in [smoke_checks.md](smoke_checks.md); an old completion
bit cannot satisfy this check.
`--require-postgres-drain` additionally waits for a successful post-test flush
and an empty local delivery outbox; query the central ledger by the report's
event ID to prove the remote row and uniqueness.

The collector can also run explicit, opt-in target-Mac exercises. These
operations restart or signal only the exact registered
`com.mnemosyne.inference.agent` job; they never find or kill a process by port:

```bash
python3 macos/packaging/collect_acceptance.py \
  --live --require-live \
  --exercise-service-restart \
  --exercise-reconcile \
  --self-test protected-vision-alias \
  --expected-engine llama.cpp \
  --require-vision \
  --require-protected-model \
  --require-download-lifecycle \
  --require-postgres-drain \
  --output "$HOME/Desktop/unified-inference-target-mac-acceptance.json"
```

Use `--exercise-keepalive` instead of `--exercise-service-restart` for the
unexpected-exit recovery pass. A protected-model result requires a
Finder-created receiver scope, persisted volume identity, healthy storage, and
a successful post-restart llama.cpp request. Download lifecycle acceptance is
based on the service's durable transition journal; migrated `snapshot` events
do not fabricate prior cancellation, retry, dismissal, or deletion evidence.
`--require-lmstudio-adoption <alias>` additionally requires LM Studio's
listener to be offline, the alias to be native and callable, the matching inert
migration row to be consumed, and its storage/model to remain under a
read-only LM Studio directory hint. For external oMLX, combine a restart,
`--exercise-reconcile`, `--expected-engine omlx`, and
`--require-omlx-recovery`.

A real login/reboot pass uses the accepted mode-`0600` pre-logout report as
`--require-login-cycle-baseline <report>`. The collector requires the same
host and exact candidate build, then proves that the exact registered
LaunchAgent returned with a different GUI audit-session ID and PID before both
listeners and another durable self-test pass. A kickstart or KeepAlive restart
within the same login session is deliberately insufficient.

Managed-runtime acceptance is accumulated across the deliberate update and
rollback passes described in [smoke_checks.md](smoke_checks.md). The service
keeps at most 256 fixed-field events in a mode-`0600` journal and gives each
service process an anonymous UUID. It never stores exception text. After the
new runtime and then the rolled-back runtime have each served from a new
service instance, and a corrupt inactive runtime has been rejected without
changing the baseline, collect the final proof with:

```bash
python3 macos/packaging/collect_acceptance.py \
  --live --require-live \
  --exercise-service-restart \
  --self-test your-llamacpp-alias \
  --expected-engine llama.cpp \
  --require-runtime-lifecycle llama.cpp \
  --output "$HOME/Desktop/unified-inference-runtime-acceptance.json"
```

The strict check requires an ordered `activated` → restarted
`inference_validated` → `rolled_back` → restarted `inference_validated` →
integrity/path-safety rejection chain, and confirms the original version is
still selected. oMLX is externally owned and is deliberately excluded from
this managed-runtime gate.

For a public release, `--require-distribution` additionally requires the
Developer ID identity, hardened runtime, timestamp, Sparkle public key/HTTPS
feed, notarization staple, and Gatekeeper acceptance for both app and DMG.

## CI release credentials

The `macOS signed release` workflow accepts only a signed
`vMAJOR.MINOR.PATCH` tag and requires these GitHub Actions secrets:

- `MACOS_DEVELOPER_ID_P12` and `MACOS_DEVELOPER_ID_PASSWORD`
- `MACOS_DEVELOPER_ID_IDENTITY`
- `MACOS_CI_KEYCHAIN_PASSWORD`
- `APPLE_NOTARY_KEY_ID`, `APPLE_NOTARY_ISSUER_ID`, and
  `APPLE_NOTARY_PRIVATE_KEY`
- `SPARKLE_PUBLIC_ED_KEY` and `SPARKLE_PRIVATE_ED_KEY`

Generate the Sparkle key once with the `generate_keys` binary from the pinned
Sparkle artifact. Store the private export only in the release secret store;
the public key is injected into the signed app. Never commit either Apple
credential or the Sparkle private key.

The workflow reruns all native gates, exports both locked Python layers, signs
nested code from the inside out, notarizes/staples/Gatekeeper-assesses the app
before imaging it, then does the same for the DMG, generates an EdDSA-signed
appcast, writes the acceptance report and SHA-256 checksums, and publishes all
of them as immutable GitHub release assets. The app checks the HTTPS
`releases/latest/download/appcast.xml` feed through Sparkle.

Version 0.x artifacts are published as prereleases for acceptance testing.
`verify_release.py` requires their `candidate_version` to match
`macos/VERSION`. For 1.x and later it additionally refuses to proceed unless
`acceptance/v1.json` has `release_ready: true` and every required gate is
`passed`. A tag cannot override the ledger.

## Update failure and rollback

Sparkle validates the EdDSA signature and Apple code identity and performs an
atomic replacement. A download, signature, extraction, or install failure must
leave the current application in place.

Every release keeps its notarized DMG. To roll back after an application-level
regression:

1. Disable automatic update checks temporarily.
2. Download the previous DMG from its immutable GitHub release and verify its
   published SHA-256 checksum.
3. Quit the menu app. The background inference service may continue until the
   prior app refreshes its LaunchAgent registration.
4. Replace only `/Applications/Unified Inference.app` with the notarized prior
   bundle, then launch it and complete the registration refresh.
5. Run Setup & Health and the real self-test before deleting the newer bundle.

The rollback does not remove `~/Library/Application Support/Mnemosyne`; model
weights, configuration, runtime installs, security-scope records, local usage,
and the Postgres outbox remain intact. If the older app does not support the
current configuration schema, it must refuse to save rather than overwrite
unknown fields.
