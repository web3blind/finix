# Native Windows quickstart

Native Windows support uses NTFS handles, owner/DACL checks, pinned executable
resolution and reparse-point rejection; it is not a POSIX compatibility shim.
Do not use FAT/exFAT, network shares, junctions/symlinks, relative tool directories,
or an elevated account to bypass an ACL failure. WSL is not this installation.
First-party licensing is confirmed MIT; third-party terms remain separate.
This experimental beta includes the reviewed runtime composition. Archive
decryption passed, but actual Desktop preview timed out before Plan/Apply;
completed Windows restoration is not claimed. See [current status](PUBLICATION-STATUS.md).

## Tools and separate Python environment

Install Python 3.11–3.14 from [python.org](https://www.python.org/downloads/windows/)
or your approved package manager. Obtain age/age-keygen from the
[official age releases](https://github.com/FiloSottile/age/releases). Optional
rclone comes from [official downloads](https://rclone.org/downloads/).
Verify the exact selected release against its published checksums/signatures;
no invented download digest/version is provided here. This packaging run did not
install or test age or rclone. Do not execute a downloaded installer from chat.

Place reviewed `age.exe`, `age-keygen.exe` (and optional `rclone.exe`) in the
owner-controlled `%LOCALAPPDATA%\Finix\tools` directory or a trusted **absolute**
PATH directory. Finix prefers the installed tool directory, pins the executable
and ancestors, and rejects unsafe owners/DACLs. It never intentionally searches
the current working directory. Do not grant Everyone/Users write access. Review
`Get-Acl`/`icacls` results; do not recursively alter existing profile/tool trees.

PowerShell examples (substitute your reviewed absolute paths):

```powershell
py -3.14 -m venv C:\FinixLab\venv
& C:\FinixLab\venv\Scripts\python.exe -m pip install C:\FinixLab\finix-0.1.0-py3-none-any.whl
& C:\FinixLab\venv\Scripts\python.exe -m finix --help
Get-Command age.exe, age-keygen.exe | Select-Object Source
& "$env:LOCALAPPDATA\Finix\tools\age.exe" --version
```

Pip installs the PyYAML runtime dependency. For offline installation provide an
approved wheelhouse and use `--no-index --find-links` with its absolute path.
Check SHA256SUMS with `Get-FileHash -Algorithm SHA256`; this alone does not
establish publisher authenticity. Install into the new venv, not live Hermes.
The wheel-installed native payload is under `<venv>\share\finix`; the native ZIP
has the same root layout. Preserve it intact for an owner-approved Hermes plugin
installation; do not overwrite an existing plugin or enable it during testing.

## Owner-only fixture directories

Create a **new** lab root on local NTFS. Inspect the resulting DACL, including
inherited entries. An example for a new directory owned by your current account:

```powershell
New-Item -ItemType Directory C:\FinixPrivateLab
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls C:\FinixPrivateLab /inheritance:r /grant:r "${owner}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F"
icacls C:\FinixPrivateLab
```

This is not a command for repairing an existing shared tree. Finix checks actual
ownership/ACLs; same-user processes can still read your files. Create synthetic
`source`, `source\cache\finix\sqlite-temp`, `recovery` directories under that
private root. On the recovery device generate a key; copy only its recipient to
the backup host. Use separate output/state paths and a new restore target:

```powershell
& C:\FinixLab\venv\Scripts\python.exe -m finix keygen --identity C:\FinixPrivateLab\recovery\recovery.key
& C:\FinixLab\venv\Scripts\python.exe -m finix backup --source C:\FinixPrivateLab\source --output C:\FinixPrivateLab\copies --state C:\FinixPrivateLab\state --recipient age1REPLACE_WITH_PUBLIC_RECIPIENT --sqlite-temp-dir C:\FinixPrivateLab\source\cache\finix\sqlite-temp
& C:\FinixLab\venv\Scripts\python.exe -m finix status --output C:\FinixPrivateLab\copies --state C:\FinixPrivateLab\state
& C:\FinixLab\venv\Scripts\python.exe -m finix restore --archive C:\FinixPrivateLab\copies\REPLACE.age --identity C:\FinixPrivateLab\recovery\recovery.key --target C:\FinixPrivateLab\restored
```

Never send the recovery file to the backend, model or chat. These are setup
instructions, not a claim that this packaging run performed a native encryption
round trip or audited your ACLs. Compare fixture bytes before real use. See the
[release verification matrix](RELEASE.md) and [safety](SAFETY.md).
