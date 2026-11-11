# Recovered-profile import and activation handoff

## Adapter contract

```python
from finix.recovery_import import RecoveryImporter, RecoveryImportError

receipt = RecoveryImporter(recovery_service).import_job(
    job_id,
    profile_id=authenticated_profile_id,
    activation=server_owned_activation_fence,
    name=request.name,
    consent_import=request.consent_import,
    acknowledge_incomplete=request.acknowledge_incomplete,
)
```

Wire to `POST /recovery/jobs/{id}/import` with JSON
`{profile_id, name, consent_import: true, acknowledge_incomplete?: boolean}`.
The adapter, not the browser, supplies `activation`. Enforce the existing authenticated
profile binding and explicit same-computer/local connection policy before invoking.
The importer accepts **no client filesystem paths**. It obtains its candidate only
from the completed, fenced `RecoveryService.handoff` and its matching job status.
The service fence lock is held through publication. Do not call it while holding an
unrelated adapter mutex that can deadlock cancellation/close.

Success is exactly `{name, can_activate: true, complete, omitted_count, parked: true}`.
`can_activate` means **offer a separate confirmed host lifecycle action**, not that a
gateway is running, a provider credential works, or a restored integration is enabled.
Incomplete recoveries require the additional literal boolean acknowledgement;
the receipt and private metadata retain `complete: false` and the omission count.
Unknown/contradictory completeness is rejected. Report only `RecoveryImportError.code`
to HTTP clients; never raw exception messages, archive/config paths, or worker stderr.

Important codes: `IMPORT_CONSENT_REQUIRED`, `INCOMPLETE_ACK_REQUIRED`, `INVALID_NAME`,
`INVALID_CONFIG`, `CONFIG_NOT_READY`, `ENV_REVIEW_REQUIRED`, `PROFILE_EXISTS`,
`INVALID_CANDIDATE`, `NOT_FOUND`, `STALE_PROFILE`, `CLOSED`, `HOST_UNAVAILABLE`,
`HOST_IMPORT_FAILED`, `HOST_VERIFICATION_FAILED`, `IMPORT_FAILED`.
Names are validated literally using the host validator: no repair, shell interpolation,
create-then-restore, adoption of nonempty directories, or overwrite of `default`/existing profiles.

## Publication and safety policy

Only the main profile payload is converted. Bundle extras and the original recovery
candidate remain untouched and staged for explicit manual import. The candidate must
be exactly the bundle root (legacy/profile layout) or its `profile` child, disjoint
from all service-protected roots.

The portable, one-profile-root tar is created beneath the owning profile's private,
backup-excluded `cache/finix/import/<operation>/`. Native directory/regular-file guards
are reused; Linux traversal uses pinned directory descriptors, Windows traverses
anchored NT handles and rejects reparses. Links, special files, case-colliding paths,
unsafe names and a conflicting `.finix-paused` tree fail closed. There is no fixed
file-count gate. File payloads are streamed. Temporary plaintext is removed on return;
process/power interruption can leave private cache contents requiring later cleanup.
Same-account processes are not a separate security boundary.

Before publication:

- Add `gateway.parked` to prevent multiplexer adoption, including an immediate rescan.
- Preserve model/provider configuration, `.env` credentials, `auth.json`, session DB,
  memories, skills, user preferences and other non-runtime profile data.
- Remove machine-specific PM/runtime/binary trees and exact foreign coordination
  records, using reviewed host layout/clone definitions. Do not delete credential
  stores just because their names contain `auth` or `lock`.
- Pause every cron job with the existing supported job fields.
- Set `plugins.enabled: []`, clear `hooks`, clear `skills.auto_load`, explicitly set
  messaging/listener adapter blocks `enabled: false` in every supported YAML location,
  and remove cross-profile routes. Reviewed bundled deferred-platform names and aliases
  are disabled even when absent from recovered config (for example Google Chat/IRC).
  Unknown newly bundled platform manifests fail the host compatibility gate rather
  than silently allowing credential-based autostart. Explicit adapter disable beats credential-based
  auto-enable in the reviewed host. Model credentials remain available for manual chat.
- Disable recovered `mcp_servers.*.enabled` and external `secrets.*.enabled` sources.
- Move executable plugin, Desktop plugin, filesystem hook and agent-hook trees, plus
  legacy `gateway.json`, under inert `.finix-paused/`. Preserve the original config
  there for review; never auto-enable Finix just to obtain status.
- Park unsafe runtime/code-injection `.env` assignments while retaining ordinary
  credential assignments. Preserve the original `.env` privately if filtering was
  necessary; unsupported multiline runtime controls require explicit settings review.

The supported `hermes_cli.profiles.import_profile(archive_path, name=...)` runs in a
short-lived local child, never via a shell or a gateway launcher. The reviewed host
uses `shutil.move`, which can adopt a raced-in directory or copy a partially visible
profile across filesystems. **Only inside this child**, its final move is guarded by
Finix's native atomic no-replace directory publication. Host extraction temp is pinned
to the private import cache, on the target filesystem; cross-filesystem publication
fails closed instead of falling back to a partial copy. No live host globals are
patched. The host importer source is compatibility-pinned by normalized-source SHA-256
`50bafad68dd4b9ef3374a8892230c7d65c75d6e86c062820a10221458f90d991`.
A different implementation is refused pending review, not silently trusted.

Readiness checks reject malformed/duplicate/aliased YAML and missing model selection.
The child also runs the host's `validate_config_structure` on the already-parsed inert
config before import. No recovered Python/JS/shell code is loaded. After host import,
the exact target file inventory and every digest are read back before success.
A private `cache/finix/import/<operation>.json` records previous profile/local source,
completeness, parked state and publication status. Failed/ambiguous targets are never
automatically deleted; failure metadata records whether a target was left for review.
No backend rollback switching occurs. A separately consented `/start` operation
uses the installed host control socket, never a gateway subprocess or active-profile switch.
Its authority is the authenticated recovery job's successful import record and the
verified imported inventory/digests; a client cannot name another existing profile.

### Tested host compatibility

The normalized importer/helper pins were independently re-read from the actual
installed runtime, not inferred from a checkout revision. Tested Desktop/SDK
semver is **0.23.0**, Windows package **2026.7411.3516.0**. The installed Python
package's `0.0.0` metadata is not a compatibility authority. Linux exercises a
byte-identical source fixture copied from that installed runtime, not a claimed
native Linux package installation. Exact normalized source hashes in
`recovery_import.HOST_SOURCE_PINS` and `recovery_activation.LIFECYCLE_PINS` are
allowlists, not user-configurable arbitrary pins; unknown implementations fail closed.
Future host/SDK versions require independent contract tests and review before support.

### Confirmed start

`POST /recovery/jobs/{id}/start` accepts exactly
`{profile_id, name, consent_start: true}`. The new name must equal the job-bound
imported name. The host must already be a live local multiplexer at the imported
profile's root; unavailable/standalone hosts fail closed, without spawning or
restarting anything. After revalidating every imported file, Finix removes only
that target's `gateway.parked`, then calls the supported
`gateway.control_socket.request_serve_profile_hot(owner.home, name)` used by the
installed host's named-profile `gateway start` lifecycle. Success requires the
exact `{served: name, served_profiles: [...name]}` ACK without error/pending, and
returns `{name, served: name, integrations_paused: true}`. Missing/pending/wrong
ACK reparks the target and reports `SERVING_NOT_CONFIRMED`; periodic host
reconciliation removes any late startup. It does not claim an immediate stop.
No restored cron, messaging, plugins, hooks, MCP or external secrets are unpaused.
A confirmed import/start record is in-memory and job-fenced, not transferable to
another process/session; after restart, no old ACK is treated as current evidence.

## Frontend activation is a separate operation

Preserve the previous `{connectionId, profile}` in frontend state. Explain that plugins,
hooks, messaging, MCP and external secret integrations remain paused pending user review.
Only after a fresh user confirmation use the supported host lifecycle to start/unpark
this named profile and require its acknowledgement, then
`await host.ensureAgent('local', receipt.name)`. Verify the supported host acknowledgement,
`host.state.profile`, both local connection IDs, and a route-pinned profile description.
Do **not** require a restored Finix REST endpoint: restored Finix is deliberately disabled.
Keep the current conversation intact. Roll back only on another explicit user choice,
using `host.ensureAgent(previous.connectionId, previous.profile)`. Do not kill/restart the
backend or overwrite the source profile to complete this flow.

## Verification

`tests/test_recovery_import.py` contains fake-host and real host importer acceptance.
Run the unit cases with `python -m unittest discover -s tests -p test_recovery_import.py -v`.
Opt into the real importer cases with `FINIX_TEST_REAL_HOST=1`, using an interpreter with
Finix and the installed host's schema dependencies, and its payload source on `PYTHONPATH`.
`age` and `age-keygen` are needed for the real authenticated recovery-to-import case.
All identities/credentials are generated fixtures. No cloud/provider/gateway is started.

Tests use only disposable roots beneath platform scratch. They override `HERMES_HOME`,
`LOCALAPPDATA`, `HOME` and `USERPROFILE` and assert the exact resolved target **before**
calling the importer. `HERMES_HOME` alone is insufficient when scratch is below the real
native Hermes root: the host intentionally anchors named profiles to that native root.
On Linux, use a private Linux filesystem scratch; a Windows mount without permission
metadata cannot satisfy mode-0700 staging, and is correctly rejected.
