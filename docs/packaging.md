# Packaging

How the planner becomes something a player can install. Targets: Linux (AppImage first; Arch and Fedora
packages later) and Windows.

## The frozen app

`scripts/build_app.py` freezes the planner with PyInstaller (`packaging/rune-advisor.spec`): Python, the
dependencies, the data tables and the window's files in one folder with one executable
(`packaging/launcher.py`). With no arguments the executable opens the planner window; with arguments it is
the CLI.

    uv run --extra ui --group package scripts/build_app.py            # refuses uncommitted changes
    uv run --extra ui --group package scripts/build_app.py --allow-dirty   # test build

The result goes to `dist/` (ignored). The bundle holds a `BUILD` file naming the version, the commit and the
data submodule's commit, so a released file can be traced to its source.

PyInstaller does not cross-build: the Linux app is built on Linux, the Windows app on Windows.

## Where an installed app keeps things

A source checkout keeps settings and state in the working tree, as before. An installed app (frozen, or a
wheel outside a checkout: `config.CHECKOUT` is None) uses the user's folders:

| | Linux | Windows |
| --- | --- | --- |
| settings (`rune_advisor.toml`, `overrides.toml`) | `~/.config/rune-advisor/` | `%APPDATA%\rune-advisor\` |
| state (archives, seen items, window prefs) | `~/.local/state/rune-advisor/` | `%LOCALAPPDATA%\rune-advisor\state\` |
| data packages | `~/.local/share/rune-advisor/data/` | `%LOCALAPPDATA%\rune-advisor\data\` |
| cache | `~/.cache/rune-advisor/` | `%LOCALAPPDATA%\rune-advisor\cache\` |

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

About 210 MB, nearly all of it Qt WebEngine (the window's browser engine, from the PyQt6 wheels).

Known limits:

- **glibc.** A build runs on systems with the same or a newer glibc than the build machine's, because
  PyInstaller collects some system libraries. Release builds must be made on an old base (an
  Ubuntu 22.04 CI runner or older); a build made on a current Arch is for testing on current systems.
- **X11 libraries.** Qt's X11 plugin needs `libxcb-cursor`, `libxcb-icccm`, `libxcb-image`, `libxcb-keysyms`,
  `libxcb-render-util`, `libxcb-util` and `libxkbcommon-x11`. PyInstaller bundles them when the build machine
  has them and only warns when it does not: the release build machine must have them installed, or the app
  will start on Wayland only.
- Checked so far: the CLI, and the window with `QT_QPA_PLATFORM=offscreen`, with an empty home folder. Not
  yet started on a real desktop.
- Not trimmed: Qt's translations and unused Qt modules could come out (roughly 60-100 MB unpacked).

## Linux: distribution packages (not built yet)

Arch (AUR `PKGBUILD`) and Fedora (COPR spec) packages would depend on the distribution's Python, PyQt6 and
Qt WebEngine and install only the wheel: a few MB, updated by the system. Both need a public source tag to
build from.

## Windows (not built yet)

The same spec freezes on Windows: pywebview uses the system's WebView2 there, so no Qt (a much smaller
bundle), with a console executable for the CLI and a windowed one for the planner. Still to do: a Windows CI
job, an installer with an uninstaller (Inno Setup), and signing.

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
