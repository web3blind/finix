# Linux quickstart

Finix first-party code is MIT licensed; third-party terms remain separate.
This is an experimental beta, not full restoration acceptance. A real Linux
encrypted backup and full readback are verified; full default replacement and
cloud restore are not claimed. See [current status](PUBLICATION-STATUS.md).

## Prerequisites

Use Linux with Python 3.11–3.14 and a trusted local filesystem supporting POSIX
permissions and safe no-replace rename. Install Python/venv using your distribution
package manager. Install age/age-keygen from your distribution or the official
[age project](https://github.com/FiloSottile/age/releases). Optional cloud transport
uses [official rclone](https://rclone.org/downloads/). Check the selected upstream
release's actual checksums/signatures using its instructions. No fixed download
URL or guessed checksum is supplied here; age/rclone were not installed or tested
by this packaging run. Keep their license notices with separately distributed tools.

Review absolute executable locations and permissions; do not add `.` or an empty
PATH entry. Executables and ancestors must not be writable by untrusted users.

```sh
command -v python3 age age-keygen
age --version
age-keygen --version
python3 -m venv /ABSOLUTE/finix-env
/ABSOLUTE/finix-env/bin/python -m pip install /ABSOLUTE/finix-0.1.0-py3-none-any.whl
/ABSOLUTE/finix-env/bin/python -m finix --help
```

Pip needs PyYAML >=6,<7 (or a separately prepared offline wheelhouse). This installs
only into the new environment, not Hermes. Verify the artifact's SHA256SUMS before
installation; checksum agreement is integrity, not publisher authenticity.

## Synthetic first backup

Use new disjoint paths, replacing **all** placeholders. The key belongs on the
recovery device; run keygen there, then transfer **only the public recipient** to
the backup host. Never print/paste the private recovery file.

```sh
umask 077
mkdir -p /ABSOLUTE/private-recovery
/ABSOLUTE/finix-env/bin/python -m finix keygen --identity /ABSOLUTE/private-recovery/recovery.key
# On the backup host, use a small synthetic source first:
mkdir -p /ABSOLUTE/sample-profile/cache/finix/sqlite-temp
chmod 700 /ABSOLUTE/sample-profile/cache/finix/sqlite-temp
/ABSOLUTE/finix-env/bin/python -m finix backup --source /ABSOLUTE/sample-profile --output /ABSOLUTE/copies --state /ABSOLUTE/copy-state --recipient age1REPLACE_WITH_PUBLIC_RECIPIENT --sqlite-temp-dir /ABSOLUTE/sample-profile/cache/finix/sqlite-temp
/ABSOLUTE/finix-env/bin/python -m finix status --output /ABSOLUTE/copies --state /ABSOLUTE/copy-state
# On the recovery device; use the exact archive returned by backup:
/ABSOLUTE/finix-env/bin/python -m finix restore --archive /ABSOLUTE/copies/REPLACE.age --identity /ABSOLUTE/private-recovery/recovery.key --target /ABSOLUTE/new-restored-profile
```

Target must be absent or empty. Compare recovered fixture bytes. Do not restore
over a running profile. No retention is enabled by these examples. Read [safety](SAFETY.md)
before selecting actual profiles, external folders or cloud storage.

## Native Hermes payload

The native candidate ZIP contains `plugin.yaml`, root `__init__.py`, `finix/`,
`dashboard/`, `desktop/` and `dist/` together. After author approval, a reviewed
local-directory installation must preserve that complete layout in the intended
profile's `plugins/finix` directory and refuse an existing destination. Do not
copy only the Python package, overwrite a working installation, or enable it
without owner consent. Use the target Hermes version's documented plugin
validation/enablement workflow. No host-version compatibility or live activation
was verified by this packaging run. See [setup](setup-flow.md).
