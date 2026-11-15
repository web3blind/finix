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
requires Hermes plugin SDK 0.23.0. This repository does not vendor the SDK.
The Node route-source fixture requires this variable; the Python pure-admission
suite explicitly skips only its SDK-producer test when it is unset. All pure
admission assertions still execute. Missing SDK tests are not a full pass.
