# Native lifecycle owner-authorization authority

Status: **design only; production remains unavailable**.

This document defines the missing per-install macOS proof authority for native
migration, rollback, and uninstall. It does not enable lifecycle execution.
The current helper is staged inside the fixed
`Contents/Helpers/MnemosyneLifecycleAuthorization.app` wrapper, but remains an
authentication transport that always refuses to emit a production proof. The
normal service constructs `NativeLifecycleJournal` without a proof authority,
and the lifecycle runner remains inert.

This boundary must stay closed until a credentialed Developer ID build, the
new provisioning profile and entitlements, mutual peer validation, key
provisioning, and representative-hardware acceptance all exist together. A
software key, environment secret, unkeyed digest, exported private key, or
successful `LAContext.evaluatePolicy` call by itself is not an acceptable
substitute.

## Security objective and boundary

The authorization receipt must prove all of the following to the running
service:

1. the exact signed helper named by the immutable execution manifest handled
   the exact one-shot service challenge;
2. macOS successfully authenticated the local device owner for that session;
3. a non-exportable key created on this Mac signed the closed receipt payload;
4. the service verified the signature against the public key pinned for this
   installation; and
5. the nonce, transaction authority, recovery clone, helper/app build,
   session, key generation, and expiry still match current durable state.

The threat model includes an authenticated loopback-control caller that must
not be able to manufacture an owner receipt, replay an earlier receipt,
substitute a helper executable, or replace the first public key during its
provisioning exchange. It also includes cancellation, timeout, service/app
restart, in-place update, rollback, missing Keychain state, and an unavailable
Secure Enclave.

Compromise of the signed service/helper code, the Developer ID private key,
the logged-in user's complete process and Application Support state, macOS
Security/LocalAuthentication, or root is outside this proof's boundary. The
public-key pin is integrity-sensitive even though it is not secret. If the
product later promises isolation from arbitrary same-UID filesystem writes,
the pin must move to a separately provisioned OS-protected service verifier;
mode `0600` state alone cannot make that stronger claim.

Owner authorization does not grant new filesystem authority. It cannot widen
the private execution manifest, move model weights, replace exact lexical
storage paths, change bookmark/scope ownership, delete ambiguous/shared/imported
weights, or invent a runner effect.

## Why the wrapper scaffold is still insufficient

The original helper was a standalone Mach-O directly below `Contents/MacOS`,
which provided no supported location for the provisioning profile that
authorizes restricted macOS Keychain entitlements. The packaging scaffold now
places that executable in the nested wrapper specified below and can validate
and embed an externally supplied exact profile. Ad-hoc and unprofiled builds
claim no helper entitlements, and a profile alone still grants no lifecycle
authority.

Apple's data-protection Keychain is the relevant implementation because
biometric/user-presence protection is a data-protection Keychain feature.
Apple documents that its access groups come from code-signing entitlements,
that those entitlements must be authorized by a provisioning profile, and
that a standalone command-line executable cannot embed that profile. Apple
requires such an executable to be wrapped in an app-like structure. See
[TN3137: On Mac keychains](https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains),
[TN3125: Inside Code Signing: Provisioning Profiles](https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles),
and
[Signing a daemon with a restricted entitlement](https://developer.apple.com/documentation/xcode/signing-a-daemon-with-a-restricted-entitlement).

There is a second independent gap: today the helper validates the connected
service Python through `LOCAL_PEERTOKEN` and Code Signing Services, but the
service transport does not validate the spawned helper's audit token/dynamic
code identity. Self-validation by the child is not sufficient for first-use
public-key pinning; an executable substituted before `exec` could return its
own public key. The first pin may be accepted only after both peers have
validated one another.

Consequently, merely supplying the wrapper/profile, adding
`SecKeyCreateSignature`, or persisting public bytes returned from the current
one-way transport would not establish a production authority.

## Required packaged identity

The authorization helper becomes an on-demand nested app-like wrapper, for
example:

```text
Unified Inference.app/
  Contents/
    Helpers/
      MnemosyneLifecycleAuthorization.app/
        Contents/
          Info.plist
          embedded.provisionprofile
          MacOS/
            mnemosyne-lifecycle-helper
```

The final name is less important than keeping all of these identities exact
and stable:

- `CFBundleIdentifier` and the helper executable's designated identifier are
  `com.mnemosyne.inference.lifecycle-helper`;
- the Developer ID Team Identifier matches the outer app, service Python,
  recovery clone, and sealed peer manifest;
- the helper wrapper has its own explicit App ID and Developer ID Application
  provisioning profile;
- the profile is embedded at the nested wrapper's
  `Contents/embedded.provisionprofile` and authorizes the exact claimed
  entitlements;
- the helper executable is signed with hardened runtime and a secure
  timestamp, the wrapper is signed after its executable/profile, and the
  outer app is signed last so its resource seal covers the complete wrapper;
  and
- the service LaunchAgent continues to use the existing direct
  `Contents/MacOS/mnemosyne-service-bootstrap`. The wrapper is only for the
  on-demand authorization helper and must not change `SMAppService`'s
  `BundleProgram` or introduce another persistent job.

The helper's credentialed signature must claim exactly the profile-authorized
values for:

- `com.apple.application-identifier` on macOS;
- `com.apple.developer.team-identifier`; and
- `keychain-access-groups`, containing one dedicated group derived from the
  Team ID and helper App ID.

It does not need App Sandbox, an application group, network access, a named
listener, a privileged daemon, or a shared keychain group with the menu app.
The build must not synthesize Team IDs or profiles. Credentialed CI supplies
the exact profile outside the repository; ad-hoc/local builds omit the
restricted authority and remain unavailable.

Release verification must use Apple's signing/profile tools rather than
parsing undocumented signature internals. It must reject a claimed production
authority when the wrapper, profile, entitlement allowlist, Team/App ID,
expiry, Developer ID identity, hardened runtime, timestamp, nested seal,
notarization, or Gatekeeper assessment is missing or inconsistent.

## Key and receipt construction

The helper owns one per-user, per-installation NIST P-256 signing key. It
creates the private key on the Secure Enclave with Security framework key
generation, makes it permanent in the data-protection Keychain, and applies
access control that requires both private-key usage and user presence with
`kSecAttrAccessibleWhenUnlockedThisDeviceOnly`. Every add, lookup, and delete
query explicitly selects the data-protection Keychain and the dedicated
access group.

Apple documents that Secure Enclave private keys are created on the device,
cannot be imported, and are not available as plaintext key material. Only the
derived public key is exported. See
[Protecting keys with the Secure Enclave](https://developer.apple.com/documentation/security/protecting-keys-with-the-secure-enclave),
[`kSecAttrTokenIDSecureEnclave`](https://developer.apple.com/documentation/security/ksecattrtokenidsecureenclave),
and
[`SecKeyCopyExternalRepresentation`](https://developer.apple.com/documentation/security/1643698-seckeycopyexternalrepresentation).

The access-control user-presence constraint accepts system biometrics or the
device passcode/password. It must guard the key operation itself, not merely
an unrelated prompt. The helper creates a fresh `LAContext` for one session,
evaluates device-owner authentication, passes that same authenticated context
to the Keychain operation with `kSecUseAuthenticationContext`, signs once,
and invalidates the context. Apple documents both
[`userPresence`](https://developer.apple.com/documentation/security/secaccesscontrolcreateflags/userpresence)
and
[`kSecUseAuthenticationContext`](https://developer.apple.com/documentation/security/ksecuseauthenticationcontext).

The closed algorithm contract is:

- key: Secure Enclave P-256;
- proof algorithm identifier:
  `secure-enclave-p256-ecdsa-sha256-v1`;
- public representation: exactly 65-byte ANSI X9.63 uncompressed P-256;
- key ID: `sha256:` plus SHA-256 of that exact public representation;
- signed bytes: the existing
  `mnemosyne-lifecycle-helper-proof-v1` canonical payload; and
- signature: strict X9.62 DER ECDSA over SHA-256, encoded as unpadded
  base64url for the JSON receipt.

No private representation, CryptoKit key data representation, symmetric
fallback, seed, recovery phrase, or signing secret crosses the helper socket,
enters Application Support, appears in the environment, or ships in the app.

## Mutual peer ceremony and first pin

The inherited unnamed socket remains the only transport. The bounded session
adds a pre-challenge handshake:

1. The service opens the socketpair and launches only the bootstrap-pinned
   wrapper executable with a minimal environment and no caller-provided
   arguments other than the inherited descriptor.
2. The helper verifies its wrapper, provisioning/signing identity, outer app
   seal, sealed role manifest, and connected service Python exactly as the
   current helper verifies its side of the boundary.
3. After the child's `exec`, the helper sends a fixed ready frame. The service
   reads `LOCAL_PEERTOKEN` from its socket endpoint and uses Code Signing
   Services to validate the live peer against the sealed wrapper path,
   identifier, Team ID, CDHash, and designated-requirement digest. PID or path
   inspection alone is not accepted.
4. Only after mutual validation may the service send either a provisioning
   transcript or the exact journal-derived authorization challenge. A failed
   check terminates the complete helper process group and invalidates the
   session.

On first provisioning, the service contributes a random nonce, installation
UUID, authority generation `1`, expected helper/app identities, and an expiry.
The helper creates or retrieves the Keychain key and signs that complete
provisioning transcript using the user-presence-controlled key. The service
validates the returned public point, key ID, transcript, expiry, and signature
while the mutually authenticated channel is still live. It then atomically
pins a strict, path-free record below private lifecycle state containing only:

- schema and installation IDs;
- authority generation, algorithm, public point, and key ID;
- helper App ID, Team ID, access-group ID, and origin build/requirement
  digests;
- provisioning nonce/timestamps and transcript signature; and
- fixed lifecycle/recovery state.

If a pin already exists, provisioning is an exact replay or a conflict; it
never silently replaces the key. A valid pin constructs the service's
public-only `HelperAuthorizationProofAuthority`. Receipt verification uses a
strict P-256 parser and ECDSA verifier from the locked service runtime.

The public `/authorization/challenge` and `/authorization/receipt` seams are
useful for hermetic protocol tests but must not provide a production bypass.
Production owner authorization is accepted only inside the authenticated
`/authorization/perform` session after live service-side peer validation. An
ambiguous SQLite response is resolved from durable challenge/receipt status;
it is never repaired by accepting a caller-supplied receipt later.

## Update, rotation, loss, and removal

An ordinary signed app update does not rotate the per-install key. The helper
App ID, Team ID, and Keychain access group stay stable, while the sealed peer
manifest pins the exact CDHash/build used for each session. Both the candidate
and recovery clone must be able to use the same authority generation before a
migration can become destructive.

Rotation is a distinct owner-authorized transaction, never an update side
effect. It is allowed only with no pending challenge, no nonterminal lifecycle
executor, and no unresolved recovery claim. The old key signs a rotation
statement binding the installation ID, old and new key IDs, consecutive
generation, current signed helper identity, and expiry; the new key also signs
its provisioning statement under user presence. The service atomically pins
the new generation and retains the old public record only for bounded audit
and already-terminal verification.

If the private key, profile authority, Secure Enclave, or pin is missing or
inconsistent, authorization becomes unavailable. The service must not create
a replacement key while a pending/authorized transaction or execution claim
exists. Explicit recovery first cancels every unconsumed challenge, moves any
incomplete lifecycle transaction to manual recovery, proves no effects can
start, and then performs a new mutually authenticated owner-provisioning
ceremony with an incremented recovery generation. Loss recovery is never
reported as cryptographic rotation from the old key.

Because the key is device-bound, copying Application Support to another Mac
does not transfer authority. The destination creates a new installation/key
after explicit owner authorization; copied pins and receipts remain inert.

Keeping Application Support during uninstall keeps the authority so a signed
reinstall can resume recovery. Full removal may delete the exact Keychain item
only as a final named effect after the lifecycle transaction is terminal and
all recovery receipts are durable. This design grants no authority to delete
model weights or change their exact storage locations.

## Release and hardware gates

Hermetic tests are necessary but cannot prove this feature. Before
`authorization_available` can become true in production, all of the following
must pass:

- the current proof-less helper and every ad-hoc/missing-profile build remain
  unavailable;
- packaging verification rejects the wrong/missing nested profile,
  entitlements, App ID, Team ID, access group, helper path, CDHash,
  requirement, hardened runtime, timestamp, seal, or notarization;
- both socket peers reject a substituted executable, wrong audit token,
  pre-`exec` identity, second frame, named socket, mismatched clone, and stale
  build manifest;
- first provisioning requires owner presence, produces one stable public key,
  refuses replacement, and never exposes private key material;
- cancel, authentication failure, timeout, process death, malformed public
  point/signature, wrong key ID, expiry, replay, and caller-submitted receipt
  all fail closed without authorizing the transaction;
- the same key verifies after service restart, login, in-place signed update,
  rollback, and recovery-clone launch on representative Apple Silicon Macs;
- key deletion/reset, unavailable Secure Enclave, locked Keychain, profile
  failure, and copied state from a different Mac enter explicit recovery and
  never select a software fallback; and
- explicit rotation, lost-key recovery, keep-state uninstall, reinstall, and
  full-removal key deletion preserve the journal fences and do not alter any
  model, runtime, usage history, pairing state, bookmark, or exact weight
  path.

Even after these authorization gates pass, migration/uninstall execution must
remain unavailable until the separately reviewed closed effects runner and
its recovery acceptance are complete.
