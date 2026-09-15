# Email Automation 1.2.4

## Release scope

This is a full Windows x64 installer release for Windows 10 22H2 and Windows
11 22H2 or later. It is an in-place upgrade using the existing AppId; customer
business data remains under `%LOCALAPPDATA%` and is not replaced by the installer.

## Packaging hardening

- Bundles Python, Node/npm, Python packages, Next runtime, Electron, TACWork
  Server, OpenCode Engine and TACWork Web; startup does not run pip, npm or
  create a virtual environment.
- Embeds a pinned, Microsoft-signed x64 Visual C++ Redistributable and installs
  it silently before first app launch.
- Builds TACWork from a clean temporary worktree at `Dev` commit `a8a6156` and
  records branch, commit and clean status in its runtime manifest.
- Electron loads `app://email-automation`; the packaged `.next-prod` transition
  Web runtime remains available only for `start-stack.bat` compatibility.
- Formal builds require a clean exact `v1.2.4` tag, a verified VC++ asset and a
  valid Authenticode signature. For controlled Win10/Win11 acceptance only,
  `build-installer.ps1 -AcceptanceCandidate` permits an unsigned clean build
  without the release tag and marks its report as internal-only.
- File deletes and renames are release-gated through
  `installer/obsolete-files-1.2.4.txt`; no broad install-directory cleanup is
  used during upgrades.

## First use

The app can open without Gmail or AI configuration. A customer must separately
import its own Google Desktop OAuth `credentials.json` and enter its own Email
Automation AI provider, model and API key. Neither customer credentials nor
build-machine credentials are included in the installer.
