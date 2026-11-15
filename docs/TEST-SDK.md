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
