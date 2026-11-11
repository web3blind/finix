# Physical-default maintenance in Desktop

Physical-default maintenance is a **separate Finix page**, available through the sidebar or the command **Finix: physical-default maintenance**. It is not a mode of ordinary Restore. Ordinary Restore keeps its backup selector, local recovery-key picker and one Restore button, creates a new usable profile, selects it automatically and preserves the previous profile.

## Current availability

The reviewed server gate is enabled, subject to the exact ownership, consent and inert/default-only checks. This is experimental, not a completed live-restoration claim. The actual Windows Desktop cold-preview attempt timed out before Plan/Apply; no restored history was proved. Unknown acknowledgements require manual reopen/reconciliation, not replay. Preparing a preview does not stop or overwrite the default. See [current status](PUBLICATION-STATUS.md).

A named profile called or selected as a default is not sufficient. The server resolves the actual SDK default root and `get_profile_dir('default')`, checks that they identify the same native directory as the authenticated source home, and retains the existing native owner/source/activation fences. A client profile name, boolean, path or global storage record never grants default authority.

## Workflow

1. Open the separate maintenance contribution from the local Finix source. The server must admit its real physical-default role, verified idle owner and restricted inert configuration.
2. Select a complete backup and a recovery-key file with the native file picker. No destination, profile name, override path, technical setup or consent checkbox is required.
3. Choose **Prepare physical-default maintenance preview**. The genuine default-only `RecoveryService` downloads, decrypts and verifies into registered private staging. It is not the ordinary recovered-profile import/start route.
4. Review the sealed plan. The preview displays its plan digest and opaque native-root binding. It discloses replacement of profile-owned memories and skills, removal of old-only members, retained shared resources, downtime, old-data rollback, failed-start late-write custody, complete-copy storage cost, indefinite retention until separate cleanup, and inert-scope restrictions. Counts describe the sealed existing-member census; opaque retained host subtrees are not claimed as recursively cloned data.
5. Only after a separately approved server/client admission may the one clearly labelled **Confirm maintenance: replace this physical default** action submit fresh consent for that exact currently displayed plan. The existing server confirmation challenge binds its root, plan, source epoch and runtime generation. A stale view, candidate, scope, root binding, policy or consent is refused rather than silently re-prepared.

Recovered executable integrations remain parked. The existing exact digest-pinned exemption is only for the current trusted Finix control plane, not archived plugins. Finix does not enable archived plugins to prove status. Active shared autoruns, supervisors and sibling-serving multiplexers remain unsupported. Maintenance does not rename the entire Hermes root, stop unrelated owners, promote sibling profiles or replace shared installations.

## Existing HTTP composition

These routes run under the host's authenticated profile-scoped mount, not an unauthenticated standalone server. The existing loopback guard, strict request-body fields, duplicate-key rejection, bounded Content-Length and host ticket/CSRF handling remain unchanged. All source maintenance work precedes any foreground profile transition; this client makes no profile transition.

| Stage | Actual request |
|---|---|
| Admission | `GET /recovery/default/capabilities` |
| Source identity | `GET /recovery/capabilities` |
| Discovery | `GET /recovery/catalog` |
| Inert decryption preparation | `POST /recovery/default/prepare-auto` with `{profile_id, candidate_id, consent_restore: true}` |
| Private key | `POST /recovery/jobs/{id}/key`, one bounded multipart `file`, maximum 16 KiB |
| Exact decryption job | `GET /recovery/jobs/{id}` |
| Sealed maintenance preview | `POST /recovery/default/prepare` with `{profile_id, recovery_job, consent_prepare: true}` |
| Separate confirmation | `POST /recovery/default/apply` with `{profile_id, id, confirmation, consent_apply: true}`; currently server-refused |
| Exact maintenance status | `GET /recovery/default/status/{id}` |
| Supported cancellation | `POST /recovery/jobs/{id}/cancel`, only while decryption is awaiting a key or running |

The new `default-maintenance-preview-v1` projection is produced from the sealed server plan, not reconstructed in Desktop. It contains the plan digest, hashed native-root comparison binding, literal `replace-real-default` scope, reviewed replacement-policy identifier, fixed replacement-root names, nonnegative numeric census counts and explicit opaque-inventory/inert restrictions. Preservation policy fields remain versioned. Native paths, raw plan inventories, provider data, credentials, tracebacks and arbitrary server messages are not displayed.

## Uncertain acknowledgement and reopening

The controller claims ownership before awaiting a mutation. It never retries prepare, key, preview or physical Apply merely because its acknowledgement was lost. Known jobs are read by their exact identifier. The public API has no durable request-id lookup for a lost initial preparation or preview acknowledgement: those cases remain explicitly blocked for reconciliation, not represented as successfully reattached operations. A nonsecret global hint can request an exact authenticated status read after reopening, but cannot authorize key dispatch, a new plan, Apply or a profile switch.

Unmount clears transient key material without cancelling or releasing backend ownership. Scope/epoch changes invalidate captured callbacks and consent. Clearing a key while its local read is pending prevents late dispatch and wipes the resulting mutable buffer. Cancellation is offered only for the backend's supported decryption stages. Detached physical maintenance has no cancel control or automatic replay.

A backend `ready` status alone is not displayed as proven usable recovery. The client also requires the exact local SDK default route, `profiles.describe(name='default')` and a successful stock `session.list(scope='stored')` response. It does not claim full historical metadata equality or AI-turn readiness. Failure to verify that route is explicit. Old-default rollback readiness is reported as a failed maintenance with preserved custody, not as a successful replacement. Manual and reconciliation outcomes preserve data and block duplicate Apply.

## Acceptance boundary

Client unit/DOM-model tests are not native installed Desktop acceptance. Generated-identity HTTP tests use the real parser, exact service, age decryption, cold sealed plan and public projection, but explicitly model the live daemon preparation seam; they never fabricate a real SDK READY acknowledgement. Independent review, public-gate admission, exact frozen packaging and synthetic native Windows/Linux full-UI maintenance acceptance remain separate gates. Never test this operation against a personal default or enable it as part of ordinary Restore.
