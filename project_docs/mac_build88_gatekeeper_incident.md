# Build 88: Athena rejects the installed app

Observed and recovered on 2026-09-09 through the user's existing Athena Screen
Sharing session. This is a target-host incident record, not a general release
approval or an image-inference acceptance result.

## Observations

- Athena's Desktop build 88 DMG had SHA-256
  `a155fb1e8c594d9d4ec46893dd4efc0a36fa76b37a7152b20ebf615dabc78e1d`,
  matching the signed, notarized, and stapled release artifact.
- Deep strict `codesign` verification accepted the installed application.
- `gktool scan` rejected the installed app with “software has been altered,”
  while accepting the app on the mounted DMG.
- An `rsync -nrcli --delete` dry run between the mounted and installed app
  reported no file-content, missing/extra-file, or symlink differences. This
  check did not compare extended attributes, ACLs, kernel state, or policy caches.
- A fresh `ditto` copy into a separate Applications sibling directory passed
  Gatekeeper on Athena. No quarantine/provenance attributes were removed, code
  was not re-signed locally, and system policy was not overridden.

## Recovery performed

The rejected bundle was retained on Athena at
`~/Desktop/Unified-Inference-Recovery-20260909-1641/Unified Inference.app`.
The verified fresh bundle was moved into the vacant
`/Applications/Unified Inference.app` path. Gatekeeper accepted it at that final
path. A normal launch opened build 88, and Setup & Health showed the background
service enabled and idle, all configured engines ready, and storage and models
ready. Configuration, credentials, model files, and engine installations were
not edited during this recovery.

## Interpretation and remaining work

The evidence points to state associated with the old installed file objects or
their metadata, rather than changed signed payload bytes. The precise original
trigger was not proven: there is no pre-replacement inode trace or private
Gatekeeper diagnostic tying the failure to a specific cached entry.
[Apple's update guidance](https://developer.apple.com/documentation/security/updating-mac-software)
describes signing-cache failures when signed code is modified in place and
recommends replacement with newly created files. That mechanism is consistent
with this recovery, but is not itself proof of this incident's root cause.

Existing-install upgrades must be tested independently from clean installation
and fresh-copy Gatekeeper checks. Capture file identity around the actual
upgrade path and require a successful final-path scan and normal app/service
startup. A verified standalone installer or completed updater path should stage
and validate a complete fresh bundle before replacement; this incident does
not enable the repository's deliberately unavailable lifecycle executor.

## Build 89 packaging follow-up

The signed DMG now includes a separate, user-launched **Install Unified
Inference** assistant. It embeds the complete signed payload and its inventory,
creates a fresh same-volume copy, validates the source and copy, and exchanges
entire directories with `renameatx_np` instead of editing existing bundle
members. It requires a final-path Gatekeeper scan, retains the previous app,
and rolls back on failure only while the recorded directory identities still
match. It uses ordinary Applications write permission and does not call the
service's gated lifecycle executor or alter security policy.

Automated signed-artifact acceptance runs the same workflow in a disposable
Applications directory containing a copy of build 88. It checks the complete
retained inventory, old directory identity, new executable inode, and final
Gatekeeper outcome. This is more specific evidence for the new replacement
mechanism; target-host normal launch, registration refresh, and inference
remain separate checks. The full released disk-image checks are recorded
beside the Desktop artifact in `Build-89-installer-verification.json`.
