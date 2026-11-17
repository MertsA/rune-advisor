# Packaging

How the planner becomes something a player can install: an AppImage for Linux, an installer for Windows and a
disk image for macOS, all built by one script and by CI (`.github/workflows/build.yml`). Arch and Fedora
packages come later.

## The frozen app

`scripts/build_app.py` freezes the planner with PyInstaller (`packaging/rune-advisor.spec`): Python, the
dependencies, the data tables and the window's files in one folder with one executable
(`packaging/launcher.py`). With no arguments the executable opens the planner window; with arguments it is
the CLI.

    uv run --extra ui --group package scripts/build_app.py            # refuses uncommitted changes
    uv run --extra ui --group package scripts/build_app.py --allow-dirty   # test build

The result goes to `dist/` (ignored). The bundle holds a `BUILD` file (beside the bundled `LICENSE`) naming the
version, the commit and the data submodule's commit, so a released file can be traced to its source. Before
wrapping, the script starts the frozen program once with an empty home folder (`data status`); a bundle that
cannot start fails the build.

PyInstaller does not cross-build: each platform's app is built on that platform, which is what the CI jobs do.

## CI and releases

`.github/workflows/build.yml`: pytest (with the Lua scripts) on every push and pull request; on pushes to
`main` and on tags, the three build jobs, each uploading its file as a workflow artifact. A tag `v<version>`
also drafts a GitHub release with the three files, `SHA256SUMS` and a build provenance attestation for each
(`gh attestation verify <file> --repo MertsA/rune-advisor` proves a download came from this workflow).
The draft is published by hand. The version is `__version__` in `src/rune_advisor/__init__.py`: bump it (and
`pyproject.toml`) in the tagged commit. Actions are pinned to commits.

## Where an installed app keeps things

A source checkout keeps settings and state in the working tree, as before. An installed app (frozen, or a
wheel outside a checkout: `config.CHECKOUT` is None) uses the user's folders:

| | Linux | Windows | macOS (under `~/Library`) |
| --- | --- | --- | --- |
| settings (`rune_advisor.toml`, `overrides.toml`) | `~/.config/rune-advisor/` | `%APPDATA%\rune-advisor\` | `Application Support/rune-advisor/` |
| state (archives, seen items, window prefs) | `~/.local/state/rune-advisor/` | `%LOCALAPPDATA%\rune-advisor\state\` | `Application Support/rune-advisor/state/` |
| data packages | `~/.local/share/rune-advisor/data/` | `%LOCALAPPDATA%\rune-advisor\data\` | `Application Support/rune-advisor/data/` |
| cache | `~/.cache/rune-advisor/` | `%LOCALAPPDATA%\rune-advisor\cache\` | `Caches/rune-advisor/` |

Linux follows the `XDG_*_HOME` variables. A settings file in the working directory still wins, and the
`RUNE_ADVISOR_CONFIG` / `_STATE` / `_CACHE` / `_DATA_HOME` variables override everything.

## Linux: AppImage

`RuneAdvisor-<version>-x86_64.AppImage`: the pinned AppImage runtime (type2-runtime, checked by SHA-256)
followed by a zstd squashfs of the bundle plus `AppRun`, a desktop entry, the icon and AppStream metadata.
The script writes the image itself with `mksquashfs` (package `squashfs-tools`); appimagetool is not used.
File times inside the image are the commit's.

    ./RuneAdvisor-0.1.0-x86_64.AppImage                    # the window
    ./RuneAdvisor-0.1.0-x86_64.AppImage guide YourName     # the CLI
    ./RuneAdvisor-0.1.0-x86_64.AppImage --appimage-extract-and-run   # on a system without FUSE

About 150 MB, nearly all of it Qt WebEngine (the window's browser engine, from the PyQt6 wheels). The spec
trims what the window never uses: Qt's QML modules, translations (one browser locale is kept), developer
tools, embedded-device plugins and the GTK theme plugin, then every library only those needed (213 MB
before). What is left is the engine itself (about 200 MB unpacked) and its ICU data.

Known limits:

- **glibc.** A build runs on systems with the same or a newer glibc than the build machine's, because
  PyInstaller collects some system libraries. CI builds in an `ubuntu:22.04` container (glibc 2.35; the Qt
  wheels themselves need 2.34), so releases run on Ubuntu 22.04, Fedora 36, Debian 12 and newer. A build
  made on a current Arch is for testing on current systems.
- **X11 libraries.** Qt's X11 plugin loads `libxcb-cursor` and friends; PyInstaller bundles them from the
  build machine and only warns when they are missing (then the app starts on Wayland only). The CI job
  installs them; the sandbox does not have them.
- Checked so far: the CLI, and the window with `QT_QPA_PLATFORM=offscreen` (CI repeats that check), with an
  empty home folder. Not yet started on a real desktop.

## Linux: distribution packages (not built yet)

Arch (AUR `PKGBUILD`) and Fedora (COPR spec) packages would depend on the distribution's Python, PyQt6 and
Qt WebEngine and install only the wheel: a few MB, updated by the system. Both need a public source tag to
build from.

## Windows

`RuneAdvisor-<version>-windows-x64-setup.exe`. pywebview uses the system's WebView2 (part of Windows 11 and
of updated Windows 10), so there is no Qt and the bundle is small. Two executables: `Rune Advisor.exe` (the
window, no console) and `rune-advisor.exe` (the CLI). The installer is Inno Setup
(`packaging/windows/rune-advisor.iss`): per user, no administrator prompt, into
`%LOCALAPPDATA%\Programs\Rune Advisor`, with a Start menu entry and an uninstaller; settings and state are
left alone by an uninstall.

Written without a Windows machine. CI builds it and the frozen CLI passes its start check there (2026-11-16,
a 31 MB installer); nobody has run the installer or opened the window on Windows yet. Still to do: an icon, version details on the executables (SignPath wants the same product name
and version on every signed file), showing the privacy policy in the installer, signing.

## macOS

`RuneAdvisor-<version>-macos-arm64.dmg` holding `Rune Advisor.app` (Apple silicon only: that is what
GitHub's runner is; an Intel build would be a second job). pywebview uses the system's WebKit. From a
terminal the CLI is `"/Applications/Rune Advisor.app/Contents/MacOS/rune-advisor" guide YourName`.

CI builds it and the frozen CLI starts there (26 MB); the window has never been opened on a Mac. It is more
exposed than Windows: the app is only ad-hoc signed, not notarised, so macOS refuses to
open it until the user allows it under System Settings > Privacy & Security (notarising needs Apple's
developer programme, $99 a year). The plugin database's default location on macOS is a guess (Steam's
library); whether the game's plugin system runs on macOS at all has not been checked.

### Code signing

Unsigned installers get a SmartScreen warning. The free option for open source is **SignPath Foundation**
(<https://signpath.org/terms>): the certificate is theirs, so the publisher shown is "SignPath Foundation".
Their conditions, as of 2026-11-16, and where this project stands:

| Condition | Status |
| --- | --- |
| OSI-approved licence, no commercial dual licensing, for all components; no proprietary components | Code is GPL-3.0-or-later. **The data is CC BY-NC-SA (not OSI-approved) and `game_configs/` is Jagex's**: an installer that bundles it may not qualify. Open question for the application; an installer without bundled data, fetching the signed data package on first run, is the fallback |
| Already released in the form to be signed; actively maintained; "a certain verifiable reputation" for programs (no figure given; their discretion, no appeal) | Not released, repository private: apply after a public unsigned release has some users |
| Binaries built from the public repository by automated CI, verifiably; SignPath checks each signing request against the build | Needs the public repository and a CI release workflow |
| Every release approved by hand by a team member; MFA for everyone on SignPath and the source host | One maintainer: fine |
| A "Code signing policy" section on the home page and download page, with their credit sentence, team roles, and a privacy policy link | To write when applying |
| Data sent to systems the user did not name must be covered by a privacy policy shown at installation, with a way to turn it off | Policy exists; the installer must show it. The planner calls the wiki, Jagex's APIs and the data host |
| Installer has an uninstaller; no system changes without warning; no security-bypassing features | Inno Setup provides the uninstaller |
| Same product name and version on every signed file | Set in the spec's version resource |
| Unsigned third-party binaries (Python, WebView2 loader) inside the signed installer | Allowed today; they may tighten it |

Alternatives: Microsoft's Artifact Signing (was Trusted Signing; about $10 a month, publisher is your own
verified name; individual sign-up was limited to the USA and Canada and reported paused in March 2026:
check before relying on it), a commercial OV certificate (hardware token or cloud HSM, a few hundred
dollars a year), or publishing through the Microsoft Store, which signs MSIX packages itself (not looked
into). Shipping unsigned at first is also what SignPath's "already released" condition implies.
