# Recovery catalog boundary retirement

Current status: partial product, not working physical-default recovery.

Removed implementations:

- `default_recovery_lifecycle.py` and detached `default_recovery_worker.py`:
  private gateway identify/control, maintenance ownership, writer locks, stop/start,
  CLI-holder coordination and whole-core source SHA admission.
- `recovery_activation.py`: private runtime discovery/start, lifecycle imports and
  lifecycle module SHA pins; its archive digest helper now lives in `recovery_import.py`.
- `recovery_pipeline.py` / `recovery_shared_host.py`: private peer transports,
  serve-profile control, runtime ownership, private authenticated IPC/ticket flow,
  source SHA pins and execution authority.
- Automatic profile importer/worker: private host implementation inspection,
  source-pinned SessionDB rekey, host publication and intercepted import execution.
- `portable_history.complete_target` and authenticated runtime history insertion:
  private gateway client/discovery/owner identity and attach-ticket reliance.
  Offline SessionDB export/preparation remains; no public export was removed solely
  because it imports SessionDB.
- Default engine apply/rollback and runtime-attempt rollback now explicitly refuse;
  journal/provenance/custody helpers remain for retained historical evidence, not
  restart authority. Preview worker entrypoints also refuse before key access.

The shared refusal is `PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE`. HTTP refuses before
reading request bodies. CLI refuses before accessing key paths. Desktop offers
catalog preview and a notice, not a key picker or runtime controls. Existing operation
reads do not convert dead workers into new jobs or modify their saved state.

Backup and standalone verified inert extraction are retained unchanged. No automatic
stock import fallback is installed. Public ZIP import merges/overwrites, can start
services and can restore external files; named profile import does not supply the
atomic new-profile publication contract. Public stop/status are not an externally
held all-writer maintenance lease. Restoration of automatic recovery needs a new
public contract, fresh independent review and remote runtime verification.

Earlier tests and runtime reports describe the retired implementation. They are
historical evidence, not acceptance of the current unavailable path. Legacy runtime
integration suites need retirement/replacement before an unrestricted test discovery
run; the focused public-unavailable behavior tests are the current regression entry.
