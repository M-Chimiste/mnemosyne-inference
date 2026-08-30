# Compatibility catalog signing and publication ceremony

This procedure creates the cryptographic material needed by the version-1
catalog without putting a private key in this repository, CI, a release app,
Nyx, or a Mac node. It is intentionally separate from the Apple Developer ID
and Sparkle authorities.

The commands use Fleet's locked Python environment because it already pins the
same `cryptography` and `jsonschema` implementations as the consumer. Prepare
that environment before taking the signing machine offline; the ceremony tool
itself performs no network operation.

## 1. Prepare the catalog body

Create a JSON file whose root is the `catalog` object from
`v1/catalog.schema.json`, not a signed envelope. Assign a sequence greater than
the last published sequence and a validity period no longer than 366 days.
Sort every identifier-keyed row and every set-like list as required by the
schema/verifier. Do not copy a discovery result directly into the catalog.

Before a live recipe is eligible for signing, retain the independent evidence
required by `project_docs/mac_pool_acceptance.md`: exact immutable weights and
file hashes, upstream revision, managed runtime fingerprint and feature
contract, supported Apple hardware/macOS classes, memory/capacity/context
results, and post-install inference evidence. The ceremony validates the
declared relationships and signature; it cannot create that evidence.

## 2. Generate an offline authority

On an offline machine, create an owner-controlled directory outside the
checkout (an encrypted offline volume is preferred). Omit `--passphrase-file`
to enter and confirm the passphrase through a no-echo terminal prompt:

```bash
uv run --project fleet --frozen python compatibility_catalog/ceremony.py \
  generate-key \
  --key-id mnemosyne-catalog-2030-a \
  --private-key /Volumes/MnemosyneCatalogOffline/mnemosyne-catalog-2030-a.pem \
  --trust-key /tmp/mnemosyne-catalog-2030-a.trust.json \
  --valid-from 1893456000 \
  --valid-until 1924992000 \
  --minimum-catalog-sequence 100 \
  --maximum-catalog-sequence 999
```

The private file is encrypted PKCS#8 and mode `0600`. The public trust-key
document contains a raw Ed25519 public key, its digest, and the exact time and
sequence window. Inspect and transfer that public document independently. It
is not secret; after the production authority and endpoint have been chosen,
its key bytes are the values pinned in Fleet and native release configuration.
Keep the private PEM and its passphrase in separate offline custody.

For non-interactive hardware/password-manager integration, a mode-`0600`,
current-user-owned regular file can be named with `--passphrase-file`. The tool
reads at most one bounded line, never prints it, and rejects symlinks or broader
permissions. Do not put that file below the checkout either.

## 3. Produce detached signatures offline

Transfer the reviewed catalog body into the offline environment and sign it:

```bash
uv run --project fleet --frozen python compatibility_catalog/ceremony.py \
  sign \
  --catalog /tmp/catalog-body.json \
  --private-key /Volumes/MnemosyneCatalogOffline/mnemosyne-catalog-2030-a.pem \
  --trust-key /tmp/mnemosyne-catalog-2030-a.trust.json \
  --signature /tmp/mnemosyne-catalog-2030-a.signature.json
```

The detached file binds the catalog identity, version, sequence, and canonical
catalog digest. It contains no private material. Multiple custodians can sign
the same body independently. A signature cannot be assembled with a changed
body.

## 4. Assemble and verify the publication bytes

On a staging host, assemble using only public trust documents and detached
signatures. Repeat `--signature` and `--trust-key` during a rotation overlap:

```bash
uv run --project fleet --frozen python compatibility_catalog/ceremony.py \
  assemble \
  --catalog /tmp/catalog-body.json \
  --signature /tmp/mnemosyne-catalog-2030-a.signature.json \
  --trust-key /tmp/mnemosyne-catalog-2030-a.trust.json \
  --output /tmp/catalog.envelope.json
```

The command refuses to overwrite output. It emits a compact verification
receipt containing the canonical catalog digest, the SHA-256 digest of the
exact envelope bytes, sequence, expiry, and verified signer IDs. It requires
every submitted signature to be recognized and valid, even though runtime
clients need only one locally trusted valid signature.

Verify those same bytes immediately before publication. Supply the previous
published envelope to reject a downgrade or same-sequence conflict, and name
every signer required for the current rotation phase:

```bash
uv run --project fleet --frozen python compatibility_catalog/ceremony.py \
  verify \
  --envelope /tmp/catalog.envelope.json \
  --trust-key /tmp/mnemosyne-catalog-2030-a.trust.json \
  --required-key-id mnemosyne-catalog-2030-a \
  --previous-envelope /tmp/previous.catalog.envelope.json \
  --receipt /tmp/catalog.publication-receipt.json
```

Publish the exact verified bytes at the configured canonical HTTPS origin and
path with `Content-Type: application/json`, identity content encoding, no
redirect, and a bounded response body. Download the resulting object into a
new local file and run `verify` again with
`--expected-envelope-sha256 sha256:<receipt value>`. This detects any transport,
CDN, templating, newline, or replacement change between staging and the served
object. Archive the catalog envelope, public trust documents, and both
receipts; never archive the private key with them.

Never replace the content of a published sequence. A correction uses a higher
sequence. During key rotation, first distribute both public anchors, publish
an overlap envelope signed by both authorities, and require both signer IDs in
the publication check. Only after supported clients carry the new anchor may a
higher-sequence catalog omit the old signer. Keep the prior trust document
available when using `--previous-envelope`, because an expired prior envelope
is cryptographically checked at its original issue time before its sequence
and digest are used as the rollback base.

## What remains external

This repository deliberately does not contain a production private key, a
fabricated production public anchor, a live catalog endpoint, or unverified
frontier-model recipes. Completing the release gate still requires an owner-
approved production public anchor and endpoint, publishing real recipes only
after their evidence passes, wiring the public anchor into signed release
configuration, and exercising update/rollback on signed artifacts and
representative Apple Silicon Macs.
