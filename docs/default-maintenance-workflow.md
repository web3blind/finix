# Physical-default maintenance in Desktop

Physical-default maintenance is a **separate Finix page**, available through the sidebar or the command **Finix: physical-default maintenance**. It is not a mode of ordinary Restore. Ordinary Restore keeps its backup selector, local recovery-key picker and one Restore button, creates a new usable profile, selects it automatically and preserves the previous profile.

## Current availability

The reviewed server gate is enabled, subject to the exact ownership, consent and inert/default-only checks. Linux local physical-default preparation and Apply have passed on the exact reviewed native package in an isolated synthetic real-default fixture, including fresh READY, stock picker/history and saved-note readback. This is experimental local proof, not cloud or Windows Desktop acceptance. The actual Windows Desktop cold-preview attempt timed out before Plan/Apply; no restored history was proved. The asynchronous preparation protocol described below does not establish that this historical live failure is resolved. Unknown acknowledgements require manual reopen/reconciliation, not replay. Preparing a preview does not stop or overwrite the default. This preparation change does not alter the gate or authorize deployment or Apply; native Windows real-default Apply acceptance remains a separate pending gate. See [current status](PUBLICATION-STATUS.md).

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
| Sealed maintenance preparation | `POST /recovery/default/prepare` with `{profile_id, recovery_job, consent_prepare: true, preview_contract: 'default-preview-job-v1'}`; prompt durable ACK |
| Exact preview preparation | `GET /recovery/default/preview/{recovery_job}`; never sends a key or repeats preparation |
| Preparation cancellation | `POST /recovery/default/preview/{recovery_job}/cancel` with `{profile_id}`; durable intent, worker retains its lease until terminal |
| Separate confirmation | `POST /recovery/default/apply` with `{profile_id, id, confirmation, consent_apply: true}`; independently gated |
| Exact maintenance status | `GET /recovery/default/status/{id}` |
| Supported cancellation | `POST /recovery/jobs/{id}/cancel`, only while decryption is awaiting a key or running |

The new `default-maintenance-preview-v1` projection is produced from the sealed server plan, not reconstructed in Desktop. It contains the plan digest, hashed native-root comparison binding, literal `replace-real-default` scope, reviewed replacement-policy identifier, fixed replacement-root names, nonnegative numeric census counts and explicit opaque-inventory/inert restrictions. Preservation policy fields remain versioned. Native paths, raw plan inventories, provider data, credentials, tracebacks and arbitrary server messages are not displayed.

## Uncertain acknowledgement and reopening

The controller claims ownership before awaiting a mutation. It never retries prepare, key, preview or physical Apply merely because its acknowledgement was lost. Once decryption has a known ID, preview preparation uses that same client-known lookup ID. A lost preview ACK permits only `GET /recovery/default/preview/{recovery_job}`. Initial decryption creation still has no request-id lookup: its unknown ACK remains locked for reconciliation. A nonsecret global hint can request an exact authenticated status read after reopening, but cannot authorize key dispatch, a new plan, renewed Apply consent or a profile switch.

The closed preparation envelope is `{contract: 'default-preview-job-v1', id, state, error, result, cancellable}`. States are running/succeeded/failed/cancelled/interrupted; only succeeded carries the existing sealed preview in `result`. Preparation ID and final maintenance ID are distinct. Capabilities advertise `preview_contract`; the matched client refuses an older synchronous server before reading a key. Older clients lacking the explicit request marker are rejected, not silently kept on an unbounded synchronous route. Original terminal failed replay never turns into a later success.

Preparation uses a 20-second monotonic **cooperative** budget for the verified-import/plan-sealing path, checked during SQLite backup callbacks (including BUSY), streaming hashes/copies and census boundaries. This is not an increased SDK timeout, a hard kernel-IO deadline, or a claim that SQLite BUSY caused a historical outage. `PREVIEW_DEADLINE`/`PREVIEW_CANCELLED` preserve uncertain staging; no implicit continuation, recapture or retry occurs. A killed worker becomes `PREVIEW_INTERRUPTED` with its ownership reserved for reconciliation. Status/cancellation use short state leases, not the expensive worker lock. Disk consumption includes inert source, complete plan payload and disk SQLite snapshot; existing storage/byte bounds still apply, without a new file-count or lifetime request limit.

Unmount clears transient key material without cancelling or releasing backend ownership. Scope/epoch changes invalidate captured callbacks and consent. Clearing a key while its local read is pending prevents late dispatch and wipes the resulting mutable buffer. Cancellation is offered for supported decryption and running preview preparation, and acknowledges only a durable cancel intent until the worker has joined. Detached physical Apply still has no cancel control or automatic replay.

## Registered CLI

`hermes finix recovery default-prepare --request-id <32-lowercase-hex> --copy-id <catalog-id> --identity <absolute-local-key-file> --consent-prepare` reserves the intent before opening the key, launches a pinned detached owner and emits the shared preparation envelope. Keep the exact request ID before dispatch. The native worker creates its own genuine service/handoff; no in-memory capability or key contents are serialized. Closing the foreground CLI does not cancel that owner. `hermes finix recovery default-preview <request-id>` is the exact-job read-only continuation; `default-preview-cancel <request-id>` records cancellation. A lost launch ACK must be reconciled with `default-preview`, not another preparation command, key submission or Apply. Repeating an exact already recorded intent returns its original durable projection without reopening the key or creating another worker. Changed intent fields are refused.

The detached worker spans download/decryption and sealed preview, not Apply. The 20-second preparation budget begins at verified-import/plan sealing, not at download start. A private key path reference is retained in owner-only intent storage for that worker; key contents are never written to the job record or output. The key file must remain available until the worker reads it. Runtime/package admission still follows the pinned SDK-selected native invocation. Process death preserves interrupted state; there is no supported automatic handoff reconstruction or dead-worker reprepare.

Native Windows and Linux fixture evidence covers file leases, generated-age/cold-plan HTTP composition and foreground CLI exit with a surviving detached owner. The shipped bundle is exercised with a modeled SDK consumer, including an actually emitted ASGI preparation envelope. Detached-process evidence models runtime selection and live daemon confirmation on both platforms; those component fixtures are not installed full-preview or post-Apply READY acceptance. The separate Linux local installed workflow now supplies that platform-specific proof, not Windows Desktop proof. Each native suite consumes its own source map; neither platform's result substitutes for the other. Neither surface bypasses physical-default admission or supports named-profile substitution.

A backend `ready` status alone is not displayed as proven usable recovery. The client also requires the exact local SDK default route, `profiles.describe(name='default')` and a successful stock `session.list(scope='stored')` response. It does not claim full historical metadata equality or AI-turn readiness. Failure to verify that route is explicit. Old-default rollback readiness is reported as a failed maintenance with preserved custody, not as a successful replacement. Manual and reconciliation outcomes preserve data and block duplicate Apply.

## Acceptance boundary

Client unit/DOM-model tests are not native installed Desktop acceptance. Generated-identity HTTP tests use the real parser, exact service, age decryption, cold sealed plan and public projection, but explicitly model the live daemon preparation seam; they never fabricate a real SDK READY acknowledgement. The separately verified Linux local installed CLI workflow reached physical-default READY and stock picker/history/note readback. Native Windows installed Desktop UI/Apply acceptance and private-cloud acceptance remain separate pending gates; component tests do not substitute for them. Ordinary POSIX source hardlink/unsafe-ancestor hardening remains separately pending and is not certified by the controlled Linux fixture. Never test this operation against a personal default or enable it as part of ordinary Restore.
