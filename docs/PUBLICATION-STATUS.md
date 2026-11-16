# Publication status — public-SDK experimental beta

## Current scope

Finix 0.1.0 provides encrypted profile backups, explicit cloud selection,
full downloaded-object verification, native CLI/Desktop setup and opt-in
scheduling. Google uses only the declared Hermes-owned `finix:google` provider
with `drive.file` and the public credentialed-provider request API.

The Workspace skill transport and private managed-gateway signing are removed.
Legacy settings, journals and cloud provenance are preserved but are not silently
migrated, adopted or replayed. Matching the same Google account does not give a
new OAuth client access to every older object under `drive.file`.

Automatic NEW named-profile import/activation and physical-default
Prepare/Apply/rollback/reopen are disabled. CLI/HTTP return
`PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE` before recovery-key access; Desktop shows
the unavailable capability without a key picker. Standalone offline extraction
to an empty directory remains available. Stock import is a manual fallback, not
exact transactional replacement. Stop/status are not a held lease on all writers.

This follows the catalog review's permitted gated-path option. Re-enabling
replacement requires a supported public contract and new implementation/review
and runtime evidence; no private SDK bridge or core-source hash pin is retained.

## Current runtime identity and real acceptance

Tested runtime source:
[`684a1e6e8694b5d66a7ee3d6b3acaae490bfd7aa`](https://github.com/web3blind/finix/commit/684a1e6e8694b5d66a7ee3d6b3acaae490bfd7aa).
Subsequent documentation-only publication commits do not constitute new runtime
builds. Hermes was released **v0.23.1** at
`3b421578588243f920886e6fe79a6faa87ea026e`. This is test identity, not a runtime
core-source compatibility hash gate.

On 2026-11-15/16, an isolated Linux VM and synthetic profile completed:

- Own Google consent and SDK-managed grant, without the Workspace skill or
  copied legacy credentials.
- Actual approved native plugin installation; installed runtime, manifest and
  Desktop payload hashes matched the published source.
- Native CLI `hermes finix check` and an actual Linux Desktop first-backup
  action, each creating a new encrypted cloud archive with zero omissions.
- Independent full downloads through the unchanged installed adapter. CLI copy:
  139,750,345 bytes, SHA256
  `a21e4d98ee7d9cea82116734b8d41fc4a711405627459c2d620a779217a30c59`.
  Desktop copy: 139,748,145 bytes, SHA256
  `b490657a17cfa632446c24dae26c39087cf4994e56b4dae34beb734d7f93bd81`.
  Each matched its expected ciphertext hash and passed real age authentication
  and full manifest validation: **19,846 files, complete=true, omitted_count=0**.
- The actual downloaded Desktop bytes passed external offline cold-data recovery
  into an absent canonical physical default-home on that VM. Normal SDK startup
  and PM regeneration succeeded. All three synthetic public SessionDB exports
  and the saved note matched; restored cron stayed disabled/paused with no next
  run. Original home and external Finix state were then returned.

No candidate-media substitution or hybrid adapter was used in these final native
runs. The earlier component experiment is separately documented in
[TEST-SDK.md](TEST-SDK.md) and is not relabelled as native evidence.

The cold test used external disposable-VM exclusivity and an existing SDK/runtime
outside the restored home. It does not prove automatic/live replacement,
application-owned writer quiescence, fresh-machine restore or production recovery.
No private recovery key was loaded into the backup UI and no schedule was enabled.
The registered public key remained locked. The paid VM was subsequently terminated
and provider billing reconciliation confirmed completion. Private evidence,
profiles, grants and recovery material are not published in this repository.

## Automated and independent checks

[Source CI run 43751550235](https://github.com/web3blind/finix/actions/runs/43751550235)
passed all four Linux/Windows focused and released-SDK validation jobs. The Linux
SDK lane executed eight history/Drive/backup-progress files: **62 passed, zero
failed**. Other focused gates cover public boundaries, locale defaults, real age,
PM aliases, native Windows owner/DACL behavior and Desktop component contracts.
See the workflow for exact files and explicit skips.

Independent source-only Claude Opus 5.5 reviews found no remaining substantive
safety/correctness blocker in the runtime delta and no findings in the final
fixture-import/CI delta. This is not maintainer approval, a whole-plugin security
certification or a substitute for execution.

## Still unverified or unavailable

- Automatic default/new-profile restoration is intentionally unavailable.
- Current native Windows GUI/cloud runtime acceptance has not been rerun. The
  successful Windows CI jobs are not a claim of live Windows Desktop acceptance.
- The unrestricted historical suite is **not green**. Diagnostic execution found
  removed-private-API dependencies, required owned-fixture scope setup not
  forwarded by the SDK's clean environment, and remaining assertion/fixture
  failures. Sibling helper namespace collisions were repaired, but the rest is
  not hidden behind blanket skips, import-error suppression or passing xfails.
- Full Google revocation/account-change/cancellation matrices and every frontend,
  optional backend, SDK version or screen-reader combination are not certified.
- The Desktop success receipt was verified independently; unknown progress
  counters and the fallback phase label are not claimed as complete UX acceptance.

The retired B11/SDK 0.23.0 Workspace-based Windows/Linux replacement results are
preserved only in [the historical record](history/acceptance-20261114.md). They
must not be presented as current SDK-only or automatic-recovery acceptance.

## Disclosure

Finix reads the selected profile and explicitly admitted external sources outside
its plugin directory. Backups can contain conversations, configuration and
credentials; encryption precedes cloud upload. External age/age-keygen processes
are required; configured storage helpers make third-party network requests.
Schedules and backup workers can run in the background. Keep private keys off the
backup host and keep independent backups. Extraction is not consent to activate
imported credentials, plugins or autoruns. No universal SDK compatibility,
hostile-concurrent-writer defense or telemetry is claimed or introduced.
