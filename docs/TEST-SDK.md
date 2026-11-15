> **Current catalog status — partial recovery only.** Automatic NEW named-profile
> import/activation and physical-default Prepare/Apply/rollback/reopen are retired.
> They depended on unsupported private Hermes gateway/CLI internals and source-SHA
> pins. CLI/HTTP return `PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE` before key access;
> Desktop displays the unavailable capability without a key picker. Old pending
> state, journals, preimages and provenance are retained, never adopted or replayed.
> Backup, source catalog/cloud previews and standalone inert offline extraction to
> an empty directory remain available. This is **not completed working default recovery**.
>
> Stock Hermes import is a **manual fallback only**, not exact transactional
> replacement: ZIP import merges/overwrites, may auto-start a service, and may
> restore external files. Named-profile import does not establish Finix's atomic
> new-profile contract. Stop/status do not prove all writers quiescent. Re-enabling
> automation requires a supported public publication/quiescence contract and new
> review/runtime evidence. Earlier success reports below describe historical builds,
> not current capability or consent to resume them.

# Optional source-integration fixtures

Set `FINIX_TEST_SDK_SOURCE` to your own checked-out Hermes SDK source directory.
The integration fixtures read the real SDK producer source without changing it.
Use the exact reviewed SDK checkout for reproducible results; native integration
requires Hermes plugin SDK 0.23.1 or later. This repository does not vendor the SDK.
Historical route-source and automatic-publication fixtures are not the current
acceptance gate. Missing or retired SDK tests are not a full pass.

## Current bounded regression gates

`.github/workflows/finix-public-api.yml` lists the exact active files and native
unittest filters. It separates public-boundary/locale tests, real age backup and
inert extraction on Linux, native Windows owner/DACL/publication tests, Desktop
component contracts, and validation with released Hermes v0.23.1 and its PM.
Portable-history preparation uses `inert_history_fixture.py`: synthetic public
SessionDB data, a fenced scratch home, no borrowed runtime or retired pipeline
teardown. Cancellation tests retain data-copy/hash/SQLite checks; an unavailable
automatic Prepare now refuses before work rather than simulating a running job.

This is a focused gate, **not a claim that the entire historical suite passes**.
Legacy controller/source-admission tests still refer to removed
`default_recovery_lifecycle`/`default_recovery_worker`; private named/shared-host
execution tests refer to removed `recovery_activation`/`recovery_pipeline`/
`recovery_shared_host`. Workspace helper-closure tests describe the removed
skill transport, not the supported `finix:google` provider. Mixed modules also
contain valid catalog, inert history and SQLite/custody invariants; they must
not be excluded solely by their filename. No blanket recovery/workspace skip,
import-error suppression or passing xfail policy has been added. Full legacy
collection and replacement transaction coverage remain unverified.

## Disposable-VM public-SDK verification

The 2026-11-15 Linux experiment used released Hermes v0.23.1, a new synthetic
profile and its own `finix:google` OAuth grant with only `drive.file`. No old
Workspace helper, copied user grant or raw-token transport was used.

Two real producer differences were reproduced before repair: PM bootstrap
virtualenv aliases outside a literal `tools/` directory (including uv's minor
version metadata), and Google's optional `session_crd` in a resumable Location.
Capture still preserves every regular file; it does not skip PM generations or
change legacy archive admission. The URI repair permits only one bounded opaque
`session_crd`, retaining the exact HTTPS origin/path and all other query fences.

Remote checks ran 14 alias tests (two named platform/tooling skips), two URI
boundary tests and 18 controlled loopback protocol tests. These fixtures are not
live Google acceptance. The opt-in SDK-created-venv test was skipped; independent
real SDK `pyvenv.cfg` and no-follow readlink captures establish the two witnessed
layouts. A hypothetical full-version uv variation is not runtime-certified.

The separate live cloud component run uploaded 92,168,673 ciphertext bytes,
independently downloaded and matched SHA256, then decrypted and validated all
13,004 archived files. It deliberately combined the unchanged installed
`dcde333` credentialed adapter with candidate capture/media handling. It is not a
native installation or GUI-backup acceptance of the final candidate.

The actual downloaded bytes also passed an externally orchestrated cold test:
absent canonical default-home, original home/state retained separately, no user
writers, normal SDK startup/PM regeneration, full equality of three synthetic
SessionDB exports and a note, and paused cron. Original directories were returned.
SDK installation/runtime stayed outside the restored home on the same VM. This
proves cold data continuity, not fresh-machine restoration, live replacement,
automatic Default Apply, sandboxing, or an application-owned writer lease.

A real older-producer/newer-reader age fixture additionally preserved a regular
PM-interpreter-named file and paused an originally enabled future cron job without
mutating its source. The installed older reader accepted the candidate-produced
cloud copy as well. Existing journals and inactive recovery state remain inert.

Actual Linux Desktop loaded the approved payload in English, preserved the
registered-key lock, verified/applied its own Google connection, and showed the
automatic-default capability as unavailable without an identity picker. Native
Windows GUI, final-candidate native CLI/UI backup, unrestricted legacy collection,
and automatic recovery remain separate, uncompleted gates. A source-only,
tool-less Claude Opus 5.5 review found no remaining substantive safety/correctness
blocker after actual-layout evidence and regression fixes; it does not replace
those acceptance gates. The chronological operator record and credential-free
runtime reports are retained privately; the workflow declares the public CI gates.
