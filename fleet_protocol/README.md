# Mnemosyne Fleet Protocol

This directory is the wire-contract boundary between independently packaged
Mnemosyne nodes and the Nyx fleet gateway.

Version 1 consists of:

- `v1/snapshot.schema.json`: the strict, secret-free node snapshot schema;
- `v1/snapshot.example.json`: a complete conforming document for consumers;
- `v1/identity_vectors.json`: canonical deployment-identity test vectors
  shared by the CUDA and native implementations.

The CUDA manager may use the repository-root `fleet_protocol.py`. The native
macOS service remains an independent package and implements the same
algorithms below `macos/service`; it must not import CUDA runtime modules.
Both implementations are expected to pass the same identity vectors.

## Compatibility

Changes that add optional implementation behavior without changing the wire
document may remain in version 1. Renaming a field, changing identity inputs,
changing canonicalization, or changing the meaning of capacity requires a new
version directory and a gateway compatibility window.

Unknown fields are rejected. This is intentional: a permissive snapshot risks
turning a future path, diagnostic, or credential field into browser-visible
state.

## Identity

`deployment_id` is lowercase SHA-256 over the UTF-8 canonical JSON form of the
complete `identity` object. Canonical JSON sorts object keys and uses no
insignificant whitespace. The `load_config_digest` is computed the same way
over a deliberately path-neutral, placement-neutral effective load
configuration.

An entry is fleet-eligible only when its immutable revision or content digest
is authoritative. Alias, node ID, local paths, GPU indices, concurrency
limits, and live state are never identity inputs.

### Version 1 load vocabulary

Load identities describe the externally meaningful model contract, not
machine placement:

- `llama.cpp`: `context_length`, `pooling`, and ordered
  `semantic_extra_args`;
- `vllm`: `context_length`, `trust_remote_code`, and ordered
  `semantic_extra_args`;
- image engines: engine/family settings plus the complete defaults applied
  when a request omits width, height, step count, or guidance.

Known concurrency and execution-placement settings—including parallel slots,
maximum sequences, thread and batch counts, GPU layers/indices, tensor split,
GPU-memory fraction, flash attention, and KV-cache placement—are excluded.
Selected model/projector files and quantization belong to `artifact`.
Unrecognized `extra_args` remain ordered inputs because an unknown flag may
change inference semantics; only recognized capacity-only arguments are
removed.

## Capacity

`effective_limit` is the lower of adapter-derived capacity and the optional
operator ceiling. `available` is admission-aware: it can be zero even when
`effective_limit - active` is positive if the node is draining, degraded,
closed to admission, or preserving FIFO fairness for a different deployment.

The node is always the final admission authority. Gateway reservations absorb
polling delay but never override a node rejection.
