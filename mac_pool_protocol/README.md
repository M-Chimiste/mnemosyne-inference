# Mnemosyne Mac Pool Protocol

This directory is the canonical, versioned wire-contract boundary for Mac Pool
inventory, advisory placement, and desired installation jobs. It is deliberately separate from
`fleet_protocol/v1`: Fleet snapshot version 1 remains the frozen routing and
admission contract, while these documents describe management-plane inventory
and work selection only.

Version 1 contains:

- `v1/mac_inventory.schema.json`: the strict, path-free `MacInventory` schema;
- `v1/mac_inventory.example.json`: a complete canonical inventory fixture;
- `v1/placement_recommendation.schema.json`: the strict, path-free advisory
  `PlacementRecommendation` result;
- `v1/placement_recommendation.example.json`: its complete canonical golden
  fixture;
- `v1/desired_install.schema.json`: the strict `DesiredInstall` schema; and
- `v1/desired_install.example.json`: a complete canonical desired-job fixture.

Every object rejects unknown fields. Arrays, strings, and numbers are bounded;
transport implementations must additionally enforce the 2 MiB inventory-sync
body limit before JSON parsing. Array ordering is normative but cannot be
expressed by JSON Schema: producers sort storage locations by
`storage_location_id`, runtimes by `engine`, installations by
`installation_id`, acknowledgements by `job_id`, and every aliases,
capabilities, and supported-versions array lexicographically or numerically.

None of these schemas grants routing authority. A fresh Fleet snapshot remains the
only live routing/admission source. `DesiredInstall` names an immutable signed
catalog recipe and an opaque storage binding; it never carries a repository
URL, local path, destination override, volume/scope/bookmark value, engine
arguments, credential, cleanup, or delete operation.
The signed catalog's digest and artifact ID also fence an exact GGUF layout,
including primary file, required shards, and selected projector when present.
Those fields deliberately do not appear in `DesiredInstall`: a mixed-version
Hub or Mac either rejects the newer catalog or observes a catalog-digest
mismatch, and must never reconstruct file roles from names or manifest order.

Placement version 1 emits one row per considered Mac and registered opaque
storage binding (or one explicit no-storage row), ranks only eligible rows, and
contains no chosen/selected target field. Each candidate basis binds the
pairing and credential generation, inventory instance and sequence, Hub receipt
and expiry, storage ID and binding generation, and signed-catalog digest. A
later explicit selection and the Mac's final local validation remain separate
authority boundaries.
