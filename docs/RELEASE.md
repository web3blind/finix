# Release candidate assembly and gates

## Experimental beta; historical build-gate notes

The author approved the MIT License for Finix first-party code, copyright 2026
Denis Skripnik. LICENSE is copied byte-for-byte from the durable approved file;
the assembler requires its SHA-256 and consistent Python/npm metadata. Third-party
components retain their own complete notices and terms; they are not relicensed.

The public beta contains the reviewed frozen runtime composition, not unreviewed
concurrent changes. See [current status](PUBLICATION-STATUS.md): publication is
not proof of full restoration acceptance. Historical build helpers may still
reject `--publish-check` under their original local policy; no runtime or script
gate is weakened here. There is no uploader or publisher in these scripts.

## Clean build

Use a disposable copy under the owning task's cache/scratch. Do not create Git
metadata, copy private configs or use a live Hermes profile. Supply a new work
path **outside** the source. Python and npm dependency preparation may contact
the network; this phase is separate from the offline test gate.

```sh
python -m venv /ABSOLUTE/release-venv
/ABSOLUTE/release-venv/bin/python -m pip install setuptools==80.10.2 wheel==0.48.0 packaging==26.3 PyYAML==6.0.3
/ABSOLUTE/release-venv/bin/python scripts/release/build.py --source /ABSOLUTE/source --work /ABSOLUTE/new-release-work --node /ABSOLUTE/node --npm-cli /ABSOLUTE/npm/bin/npm-cli.js
```

On Windows use `<venv>\Scripts\python.exe`, an absolute `node.exe`, and that Node
installation's `node_modules\npm\bin\npm-cli.js`. Node must satisfy the existing
package engine **>=22,<25**. The packaging host's default Node 20 is unsupported;
a separately verified Node 22 was used instead. Python 3.14.9, PyYAML 6.0.3,
setuptools 80.10.2, wheel 0.48.0 and packaging 26.3 were actually available in the
isolated environment. Exact Node/npm versions and installed dependency integrities
are generated in provenance, not inferred from a lockfile. No tested age/rclone
version is claimed by this packaging run.

`build.py`:

- Copies only explicit build inputs into a fresh npm tree, uses `npm ci` with
  lockfile integrity and lifecycle scripts disabled, with its own npm cache.
  `--offline-npm --npm-cache /ABSOLUTE/isolated-populated-cache` requires a
  prepopulated matching cache; it is not a claim that a first install can work
  offline. Optional platform esbuild packages are needed.
- Rebuilds Desktop and keygen twice from source, deletes generated outputs between
  passes, requires byte equality, then materializes a new public snapshot without
  modifying reviewed source files. Complete bundled notices remain embedded. Generates a separate
  third-party notice document from actually installed license files.
- Selects public inputs from `allowlist.json`, checking path components for links
  and Windows reparse points, requiring single-link regular files, and scanning
  generic private-key/token/personal-home patterns without printing matched data.
  Unknown source files are not discovered or shipped. Extending the release
  requires explicit allowlist review. No caches, local `.linux-test-*` scaffolding,
  private configuration or credential files are admitted.
- Builds the actual wheel offline without build isolation/network, using only
  the explicitly prepared isolated venv and clean public tree. The native payload
  keeps the plugin layout intact; Python and optional tools are not bundled.
- Writes sorted fixed-date uncompressed source/native ZIPs, scans every archive
  member and the wheel, and records SHA256SUMS plus input hashes, tool versions,
  dependency integrity metadata, asset hashes and rebuild provenance. A generic
  scan is defense in depth, not proof that arbitrary secrets cannot exist.

`SOURCE_DATE_EPOCH=315532800` fixes wheel timestamps. ZIP ordering/modes/timestamps
are fixed. Wheel ZIP metadata is likewise normalized to sorted, fixed-date
single regular files with mode 0644; payload bytes and RECORD stay unchanged.
This avoids writable POSIX archive modes emitted by setuptools on Windows.
Reproducibility means the same reviewed input bytes and toolchain
produce identical artifact bytes; platform/tool changes require comparison.
Provenance omits absolute working paths, usernames and hostnames. It does not
invent a Git revision when no Git metadata was supplied. Preserve the generated
manifest with the artifact; hashes are not a signature or proof of provenance
from a trusted publisher. Review and compare two actual clean builds before release.

## Final integrated build recipe (parent integration)

1. Freeze the exact merged runtime source in a new isolated source directory.
   Apply only reviewed packaging/legal/documentation changes using the private
   merge manifest's old/new hashes. Do not overwrite newer runtime files from
   this older candidate. LICENSE must match the approved durable legal text.
2. Review every changed allowlist member and explicitly add any new runtime
   module needed by the merged implementation. Compare native payload paths,
   Python package discovery and wheel inventory. An old allowlist is not proof
   that a new runtime's dependencies are complete.
3. Review executable build scripts, pyproject build backend, lockfile dependency
   URLs/integrities and the trusted toolchain before executing them. These scripts
   are not a sandbox for untrusted source or concurrent hostile writers. Build
   only from a private, frozen tree with no other writers.
4. Run the clean-build command above twice with distinct new work directories,
   the same isolated pinned toolchain and a prepared offline npm cache. Compare
   every artifact, RELEASE-MANIFEST.json and SHA256SUMS byte-for-byte.
5. Install the actual resulting wheel into a new empty environment using the
   prepared dependency wheelhouse; run CLI help and compare installed native
   payload hashes with that build's native ZIP. Run the extracted source's
   offline gate and all platform/runtime/browser acceptance gates below against
   this exact merged artifact, not this older candidate.
6. Preserve exact source/input and artifact manifests with private evidence.
   Do not mark the candidate final or publish/upload anything. MIT approval
   does not authorize publication; `--publish-check` must remain closed.

## Safe test commands and scope

```sh
python scripts/release/offline_gate.py --scratch /ABSOLUTE/existing-disposable-scratch
```

The gate clears ambient credentials/profile environment, sets an isolated home,
puts only fake age/age-keygen/rclone files on PATH, asserts their exact resolution,
then denies Python socket and process-launch audit events **before imports**.
It self-checks denial and runs packaging selftests plus a standalone core import.
On a Windows development copy that contains `tests/test_native_windows.py`, it
also runs only `test_missing_encryption_tool_retains_closed_dependency_code`.
The source release excludes legacy runtime tests; its gate reports only the
packaging tests actually present. Never silently expand this selector to the
whole test directory. The guard is a cooperative Python test boundary, not an
OS-level sandbox for hostile native extensions. All subprocesses are denied,
so provider tools cannot escape the Python socket guard as child processes.

| Platform | Supported gate | Not established by it |
| --- | --- | --- |
| Linux | Packaging selftests + core import under the offline runner | age round trip, POSIX security fixtures, real filesystem snapshot semantics |
| Native Windows NTFS | Same, plus the selected missing-tool contract in a development copy | Full native suite, ACL/handle/link behavior, real encryption/restore |
| Desktop/keygen | Rebuild byte comparison; selected Node bundle/crypto contracts may run in the isolated npm tree | Real host activation, browser pickers, accessibility, OS permissions |

Do not invoke broad Python discovery on native Windows: several older tests
construct POSIX shebang executables, use colon-separated PATH, chmod or sockets.
Their failures cannot be fixed by weakening NTFS checks. POSIX suites need a
separately reviewed Linux runner with fake executables and a true offline
boundary; the no-socket gate intentionally cannot run local HTTP fixture suites.
Node Desktop VM tests are contract tests, not native Electron acceptance.
In a prepared isolated npm tree with the corresponding source tests copied in,
the portable selected commands are:

```sh
node --experimental-vm-modules --test tests-desktop/plugin.test.mjs tests-desktop/build-portability.test.mjs tests-web/artifact.test.mjs
```

The historical `tests-web/keys.test.mjs` interop test hardcodes `/usr/bin/age`
and `/usr/bin/age-keygen`; it fails on this native Windows build host and is
not included in that supported selector. Its failure must not be reported as
successful native age interoperability. Browser tests additionally need a
separately installed browser and are not part of this packaging verification.

## Required before public release

1. MIT approval is complete; preserve LICENSE and all third-party notices.
   Separate explicit post-recording publication authorization remains required.
2. Merge reviewed parallel fixes with input-hash checks; rebuild the exact merged
   tree. A candidate built before merging is not acceptance of the merged code.
3. Clean Linux and native Windows installation with reviewed absolute tools;
   synthetic real age backup/restore byte comparison and SQLite WAL integrity.
4. Corruption/cancel/restart, incomplete capture, retention protection and visible
   progress checks on the final shipped code; inspect supported archive layouts.
5. Real Desktop layout, picker/save, keyboard/screen-reader and profile routing;
   preserve immutable public recipients and read back state changes.
6. Owner-authorized live provider upload/readback/download/full restore and
   restart/interruption checks for each claimed backend. No live accounts, tokens,
   production deployment or scheduler changes are authorized by this build.
7. Final archive inventory, secret scan, dependency integrity review, reproducible
   rebuild comparison and fresh-user instructions on each advertised platform.

A completed packaging build does not close these operational gates. Failures
and skipped tests must be recorded verbatim in local evidence, not relabeled as
successful provider/platform verification.
