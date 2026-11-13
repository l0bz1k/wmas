<p align="center"><img src="docs/image.png" alt="WMAS" width="420"></p>

# WMAS — Wayback Machine for the Mac App Store

Apple keeps old builds of Mac App Store apps on its servers, but the App Store app
only ever offers the newest one. WMAS lists the versions the Store still serves for
an app, shows the minimum macOS each one needs, and downloads the one you pick as the
original Apple-signed package.

```
$ wmas versions LanScan
LanScan — 128 versions offered by the Store
  #  Version        Released    Minimum system   Architectures    Version ID
   1  8.2.18         2026-11-02  (macOS 12.0+)    arm64,x86_64     894402100
   3  8.2.12         2026-06-29  (macOS 12.0+)    arm64,x86_64     887583330
   4  8.2.11         2026-05-29  (macOS 11.0+)    arm64,x86_64     886279823
  19  6.5.5          2020-11-10  (macOS 10.14+)   arm64,x86_64     838808549
  20  6.5.4          2020-11-03  (macOS 10.10+)   x86_64           838502038
  36  1.3            2011-11-03  (macOS 10.5+)    x86_64,i386      4431155

$ wmas versions LanScan --for-macos 11
LanScan: newest version for macOS 11 is 8.2.11 (2026-05-29, macOS 11.0+), version ID 886279823

$ wmas download LanScan 8.2.11
Done: artifacts/lanscan/LanScan-8.2.11.pkg
  signature: signed by Apple for the App Store
```

It talks to the Store directly with your own Apple ID. There is no proxy, no
certificate tampering and nothing to disable on the Mac.

## What it does

- Finds an app by name, App ID (`1476245357`, `id1476245357`) or App Store link, in
  the catalogue or in your purchases, which also covers apps pulled from sale.
- Lists the versions the Store serves, each with release date, minimum macOS and
  architectures, read from about 64 KB of the package instead of the whole download.
- Answers "what is the newest version that runs on macOS X" directly.
- Downloads any listed version, checks Apple's signature and records the SHA-256.
- Keeps several Apple IDs side by side as named profiles.
- Prints JSON on request (`--json`), so a GUI can sit on top of it.

## Requirements

- macOS on Apple Silicon or Intel (developed and tested on macOS 15, Apple Silicon)
- Python 3.9 or newer; only the standard library is used
- Go and git, once, to build the patched `ipatool`
- An Apple ID that owns the apps you want; free apps can be claimed from the tool

## Install

    git clone https://github.com/l0bz1k/wmas.git && cd wmas
    scripts/build-ipatool.sh      # clones ipatool at the pinned commit, applies our patches
    scripts/wmas                  # interactive session; sign in when asked

`scripts/wmas` can be symlinked into a directory on your `PATH`. Your password and
2FA code are typed into `ipatool` itself and never pass through WMAS.

## Usage

    wmas                                  # interactive: account, search, versions, download
    wmas search lanscan                   # catalogue search in the account's storefront
    wmas search lanscan --owned           # search purchases, incl. apps pulled from sale
    wmas versions 472226235               # versions with minimum macOS
    wmas versions LanScan --full          # probe every version; resumable, cached for good
    wmas versions LanScan --for-macos 11  # newest version that runs on macOS 11
    wmas download LanScan 8.2.11          # by version or by version ID

Apple ID profiles. A profile name is any label you choose.

    wmas accounts                                       # list profiles
    wmas accounts add us-store --email me@example.com
    wmas accounts use us-store
    wmas accounts rename default home                   # relabel; the session is untouched
    wmas accounts remove us-store
    wmas --profile us-store versions ...                # one command under another profile

In the interactive session `q` quits from any prompt and Enter goes back. Downloads
land in `artifacts/<app>/` next to a `SHA256SUMS` file.

## How the version list is built

A version's string, date and minimum macOS never change, so they are read once and
cached permanently. Each one costs a Store request, and Apple throttles those, so
`versions` does not probe everything: the minimum macOS almost never goes down
between releases, which lets it bisect for the points where the requirement changes.
For LanScan, 36 requests located all eight boundaries across 128 versions. Versions
lying between two probed ones with the same requirement are shown as *inferred*;
`--full` probes them for real.

The protocol details are in [docs/findings.md](docs/findings.md).

## Limits

- **Only versions Apple still serves can be downloaded.** For some apps that is a
  small part of the public history: Shadowrocket lists 20 versions on its App Store
  page, the Store serves 3. `versions` prints the advertised-but-not-served ones under
  the table. There is no known way to request them.
- **The Apple ID must own the app.** Paid apps you have not bought cannot be
  downloaded.
- **Packages carry no purchase receipt.** Installed with `installer -pkg`, an app
  runs without one, so in-app purchases cannot validate.
- **Apple rate-limits.** HTTP 403 on the download endpoint means "slow down", not
  "denied"; WMAS backs off and retries.
- Mac apps only for now.

## Layout

    wmas/                the tool: core.py (engine, no printing) and cli.py (terminal UI)
    scripts/             wmas launcher, build-ipatool.sh
    ipatool-fork/        our patches to ipatool and the pinned upstream commit
    docs/                protocol findings
    data/                captured protocol data
    bin/, state/, artifacts/   built binary, cache and downloads (not in git)

## About the ipatool patches

WMAS drives [ipatool](https://github.com/majd/ipatool) (MIT) with three patches kept
in `ipatool-fork/`:

- `version-probe.patch` reads a Mac package's version and minimum macOS by ranged
  request. Upstream downloads the entire package to read its version.
- `profiles.patch` isolates each Apple ID in its own keychain service and state
  directory.
- `storefront-fix.patch` falls back to a built-in storefront table when Apple's
  storefront list cannot be fetched, which otherwise breaks every command on some
  networks.

## License

MIT, see [LICENSE](LICENSE). The patches in `ipatool-fork/` modify ipatool, which is
also MIT; its notice is kept in `ipatool-fork/LICENSE.ipatool`.

## Disclaimer

WMAS is not affiliated with Apple. It downloads only what the signed-in Apple ID is
entitled to, from Apple's own servers. The packages belong to Apple and the apps'
developers; do not redistribute them. Use it within the terms that apply to your
account.
