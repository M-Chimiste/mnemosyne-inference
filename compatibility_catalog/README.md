# Mnemosyne compatibility catalog

This directory is the normative, platform-neutral trust contract for managed
Apple Silicon model compatibility. Version 1 deliberately describes only
logical models, immutable artifacts, and typed recipes for `llama.cpp`,
`omlx`, and `ds4`. It does not authorize a download, choose a Mac, select a
local storage location, mutate a runtime, or change an existing profile.

`v1/catalog.schema.json` is the closed JSON Schema for signed envelopes.
Signatures are Ed25519 over the domain-separated canonical JSON encoding of
the `catalog` member. `catalog_digest` is the lowercase SHA-256 digest of that
same canonical byte string. Implementations accept a catalog only when its
schema, content relationships, digest, time window, and at least one signature
from a locally pinned key ID all validate.

Upstream locations are identities, not caller-provided URLs. A trusted client
derives HTTPS endpoints for the fixed Hugging Face, GitHub, or PyPI registry
named by a source object. Every selected file has an exact size and SHA-256;
the artifact manifest digest covers the canonically ordered file manifest.
For GGUF and DS4 file sets, the additive `gguf_layout` member may name one
exact primary launch file, every required sibling shard, and either one
already-selected llama.cpp projector or no projector. The layout must cover
the complete artifact manifest. Projector alternatives are separate signed
artifacts/recipes; no consumer chooses one by filename convention. Catalogs
without `gguf_layout` retain the original version-1 behavior, while managed
installation accepts that legacy form only when it contains one unambiguous
GGUF file. The content-only manifest digest remains compatible with local
filesystem ownership proofs; the layout itself is covered by the catalog
digest, Ed25519 signature, and native signed-source identity.

`v1/catalog.golden.json` is signed only by the public test key recorded in
`v1/test_keys.json`. The corresponding deterministic private seed appears
only in tests. It is not a production trust anchor and must never be accepted
by a release build. `v1/catalog.gguf-layout.golden.json` is a second test-only
signed vector covering a sharded model plus one selected projector; the
original single-file golden bytes and digest remain unchanged.

The built-in fallback is an unsigned, empty, offline catalog constructed by
the verifier implementation. It is returned only when no still-valid signed
last-known-good catalog is available, and is never accepted as a downloaded
catalog.

`catalog_update.py` is the byte-identical Fleet/native HTTPS transport around
that trust core. It accepts only a canonical HTTPS origin and path, sends no
credentials, disables ambient proxy configuration and redirects, requests
identity encoding, bounds headers/body/deadlines/retries, and exposes only
fixed content-free result codes. Concurrent checks share one request. A
network, HTTP, framing, parsing, signature, expiry, conflict, or rollback
failure leaves the atomic last-known-good store unchanged. The updater is not
wired to routing, downloads, runtime management, profiles, or local storage.

`ceremony.py` is the offline production-key and publication tool. It generates
only encrypted PKCS#8 Ed25519 private keys, refuses to create or consume a
private key anywhere below the repository checkout, requires owner-only key
and passphrase-file permissions, and never accepts a passphrase in argv or
contacts a network. Individual offline signers produce digest-bound detached
signatures; a connected staging host can assemble them into a canonically
encoded envelope using public trust-key documents only. Publication
verification checks the exact envelope byte digest, every submitted signature,
required rotation signers, current validity, and optional prior-publication
sequence/digest fencing. The repository golden-vector key is explicitly
rejected by this ceremony.

See [CEREMONY.md](CEREMONY.md) for the operator procedure. The tool proves the
catalog envelope and publication ordering. It does **not** prove a model's
weights, runtime compatibility, hardware limits, context, capacity, or
post-install inference behavior, and running it does not clear the production
catalog acceptance gate by itself.
