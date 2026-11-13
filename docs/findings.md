# Protocol findings

Measured on macOS 15.8.3 (Sequoia), Apple Silicon, SIP enabled, 2026-11-12.
Test subject: LanScan, App ID `472226235`, bundle `com.iwaxx.LanScan`.

## Why stock ipatool 2.8.1 fails here

Every storefront-dependent command dies with:

    failed to resolve the country code: available storefronts request failed

Upstream commit `0d99fc2` removed the shipped static storefront table and replaced
it with a live request to:

    https://itunes.apple.com/WebObjects/MZStore.woa/wa/availableStoreFronts

That endpoint's behaviour depends on **both exit IP and User-Agent**. Measured,
three or more repetitions per cell:

| Exit | `Configurator/2.x …` (ipatool default) | `iTunes/12.6.5 (Macintosh; OS X 10.13)` |
|---|---|---|
| Russia | 404, empty body | **404, empty body** |
| Germany | 404, empty body | **200**, `text/xml`, 88869 B, 174 storefronts |

Neither factor alone explains it. The UA matters — from a permissive exit the
legacy iTunes UA gets 200 where every Configurator UA gets 404, and
`pkg/http/constants.go` sets `DefaultUserAgent` to a Configurator string, so
ipatool triggers the 404 itself even where it could succeed. Geography matters too
— from the Russian exit no UA produced a 200.

Ruled out: session cookies (a bare cookie-less curl behaves identically) and
endpoint removal (it returns 200 under the right conditions).

The captured 200 response is kept at `data/availablestorefronts-174.plist`.

### Our two fixes

`countryCodeFromStoreFront` hard-fails when discovery fails — there is no fallback.
So:

1. **Static storefront table** (`ipatool-fork/storefront_static.go`, 133 entries,
   recovered from ipatool history prior to `0d99fc2`), used when the live request
   fails. On a Russian connection this is **the only thing that makes ipatool
   work** — not optional.
2. **iTunes UA on the availableStoreFronts request.** Worth upstreaming, but it
   only helps from a permissive exit. Keep it; do not rely on it.

## What is not blocked from Russia

Only storefront discovery is. Verified on a direct Russian connection, no tunnel: platform and
version lookup (`apps.apple.com/ru/app/id…?platform=mac`), `list-versions` (128
identifiers), `get-version-metadata`, commerce (`volumeStoreDownloadProduct`), and
the asset CDN at 9.5 MB/s. Account storefront resolves to RU (`143469`) and that is
fine.

## Endpoints

- Storefront list: `itunes.apple.com/WebObjects/MZStore.woa/wa/availableStoreFronts`
- Commerce / pinned version: `p7-buy.itunes.apple.com/WebObjects/MZFinance.woa/wa/volumeStoreDownloadProduct?guid=<hex>`
  — pod number (`p7`) is per-account; `guid` derives from the machine's MAC address
- Asset CDN: `osxapps.itunes.apple.com/itunes-assets/…pkg`
- Platform version lookup: `apps.apple.com/<cc>/app/id<appid>?platform=mac`

Omitting `--external-version-id` takes a different server path (latest-version
resolution) than pinning one (redownload path).

## Packages are NOT personalized

Two independent downloads of LanScan 8.2.11 — taken from two different network exits about an hour apart — are byte-identical:

    bf331fbb9d65e03a45c963e9436d5b1515c3aaf545b72cbc4ac08a428fbfdeb3

`_MASReceipt` is absent from the payload; Apple serves a generic package and
`storeassetd` injects the receipt at install time.

Consequences: packages are safely cacheable and mirrorable, hashes are a valid
integrity check, and downloads can be deduplicated across accounts.

Installing via `installer -pkg … -target /` yields an app with no receipt at all.
Base functionality should work; in-app purchases cannot validate.

Packages verify as `signed by Apple for the App Store` with a trusted timestamp
matching the release date; the app binary is signed `Apple Mac OS Application
Signing`.

## Package structure

A xar flat package:

    Distribution
    com.iwaxx.LanScan.pkg/{Bom,Payload,PackageInfo}

`xar -xf pkg Distribution` alone yields the minimum OS and architectures:

    <volume-check><allowed-os-versions><os-version min="12.0"/></allowed-os-versions></volume-check>
    <options customize="never" require-scripts="false" hostArchitectures="arm64,x86_64"/>

`PackageInfo` carries `version`, `CFBundleVersion`, `install-location`, and a
`generator-version` of `InstallCmdsMAS-*` identifying it as a MAS package.

Payload is pbzx + xz. Use `/usr/bin/aa extract`.

## Tunnel measurement

Routing the traffic through a tunnel with a German exit unblocked storefront
discovery when paired with the iTunes UA, but degraded everything else:

| Host | Successful connects |
|---|---|
| `itunes.apple.com` | 4/5 |
| `p7-buy.itunes.apple.com` | 2/5 |
| `osxapps.itunes.apple.com` | 5/5 |

The commerce host, the critical one, was the least reliable. On the direct connection
the whole chain was reliable, so the static-table fallback, which needs no tunnel, is
the better fix. When measuring, confirm the actual exit per destination: selective
routing can send two hosts out through different countries.

## LanScan version map

Last version supporting macOS 11 is **8.2.11**. Full map in
`data/lanscan-version-map.txt`; min-OS probes in `data/min-os-probe-results.txt`.

| Version | External version ID | Release | min macOS |
|---|---|---|---|
| 8.0.27 | 881034813 | 2025-12-27 | 11.0 |
| 8.0.28 | 881193644 | 2026-01-04 | 11.0 |
| 8.2.0 | 881485768 | 2026-01-14 | 11.0 |
| 8.2.11 | 886279823 | 2026-05-29 | **11.0 (last)** |
| 8.2.12 | 887583330 | 2026-06-29 | 12.0 |
| 8.2.13 | 888634082 | 2026-07-21 | 12.0 |
| 8.2.18 | 894402100 | 2026-11-02 | 12.0 |

## Corrections and additions, 2026-11-13

**Empty metadata replies were throttling.** IDs 880277346, 880643575 and 890174900
returned nothing on 2026-11-12 and were wrongly taken for unreleased builds. They are
8.0.25, 8.0.26 and 8.2.15. A failed lookup must be retried, never cached as absent.

**The CDN body is encrypted; only the decrypted package is generic.** The asset URL
(`osxapps.itunes.apple.com/itunes-assets/...pkg`) carries no token, still answered a
day later, and honours `Range` (HTTP 206). But its bytes are not a xar: same length as
the final package, different content. ipatool decrypts the stream with an emulated
StoreAgent (`internal/sap/machine`) keyed by the hardware ID and the `dpInfo` blob from
the `volumeStoreDownloadProduct` reply, in sequential 0x8000-byte chunks.

So "not personalized" holds for the decrypted output (byte-identical across exits),
not for the transport. A mirror must store decrypted packages.

**Min-OS without a full download: proven.** PackageInfo and Distribution sit at the
*end* of the package, after the payload, not in the first chunk as first assumed. But
the 0x8000-byte chunks decrypt independently, so a probe needs only the first chunk
(XAR header and table of contents) plus the chunk-aligned tail: about 64 KB for a
10.6 MB package. Our `version-probe.patch` builds this into `get-version-metadata`.
Verified against full downloads: 8.2.11 -> 11.0 (build 1002), 8.2.12 -> 12.0.

Upstream `get-version-metadata` downloads and decrypts the whole Mac package to read
one line of PackageInfo, which is what drove the throttling on 2026-11-12.

**ipatool holds exactly one account**: keychain service `ipatool-auth.service` plus
`~/.ipatool/cookies`, both constants in `cmd/constants.go`. Multiple Apple IDs need
those two namespaced per profile.

## The Store serves a subset of the public version history (2026-11-13)

`softwareVersionExternalIdentifiers` lists only the builds Apple still offers for
download, which can be far fewer than the app's public history. Shadowrocket
(id 932747118): the App Store page lists 20 versions back to 2.2.70 (2025-09-25), the
commerce API returns three identifiers (2.2.90, 2.2.92, 2.2.93), identically for the
`macos` and `iphone` platforms. Whether Apple or the developer withdrew the rest is
not visible from outside.

The public page (`apps.apple.com/<cc>/app/id<id>?platform=mac`) carries version
strings and dates but no version identifiers, so a withdrawn build cannot be
requested: its identifier is unknown and the identifier space is too sparse to guess.

Version identifiers differ per platform for the same release (2.2.93 is 891316385 on
macOS and 891316378 on iOS).

## wmas profiles are labels

`wmas` maps a user-chosen profile label to the storage name passed as
`IPATOOL_PROFILE`, so renaming a profile only rewrites `state/config.json` and never
touches the keychain. The first profile uses ipatool's stock storage.

## iPhone and iPad apps

The same commerce API serves iOS apps (`--platform iphone` / `ipad`); for a universal
app both platforms return the same identifiers. An IPA is an ordinary zip on the CDN,
not encrypted in transit like a Mac package (only the executable inside is), so its
`Info.plist` can be read by ranged request, which upstream ipatool already does for
the version string. Our patch also returns `MinimumOSVersion`, `CFBundleVersion` and
`UIDeviceFamily` from it. One probe costs a commerce request and a few kilobytes.

Minimum iOS is monotonic in practice as well: Telegram for iPhone has 362 served
versions back to 1.0 (iOS 5.0), and bisection found the newest one for iOS 12
(11.12.1, 2025-06-06) in about ten probes.

A freshly built ipatool binary takes 30-45 s on its first launch; later launches are
fast. This looked like a hang during testing and is not one.
