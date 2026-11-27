# R6 — Custom Android system images: layout, registration, update sites, AOSP artifacts

Status: Wave 0.5 research · Owner: R6 research agent · Accessed 2026-11-26 (Europe/Dublin)
Applies to: BRIEF.md §3.3 (R-CUSTOM-IMAGE), §2 requirement "Import a custom system image
directory and boot an AVD from it", and Wave-1 agents G1x (`platform/images/`), A1
(`platform/avd/`), S1a/S1b (`platform/sdk/`), N1 (`platform/runtime/`), PT3
(`portable/relocate/`).

Labels: **VERIFIED** = checked in a primary source at the URL in §9 (Google docs, AOSP
source, or Google-published repository metadata); **PARTIAL** = inferred/partially
observed; **UNVERIFIED** = could not confirm from a primary source; treat as design
assumption to re-test on an accelerated host.

Method note: this host has no Android SDK and no hypervisor (see PLAN.md "Host constraint
discovered in Wave 0"). No SDK archive was downloaded. Archive *file lists* below were
obtained by HTTP `Range` reads of each zip's End-of-Central-Directory/central directory
(a few KiB of metadata per archive) directly from `dl.google.com`; no image bytes were
transferred and nothing was installed. This is non-destructive verification, not a boot
test. **No claim in this document is real-emulator evidence.**

---

## 1. Canonical layout under `<sdk>/system-images/`

`<sdk>/system-images/android-<api>/<tag>/<abi>/` — **VERIFIED**

- Emulator docs describe the system image directory as
  `<sdk>/system-images/<api>/<variant>/<arch>/` where `<api>` is e.g. `android-35`
  (pre-release codenames look like `android-V`), `<variant>` is e.g. `google_apis` or
  `android-wear`, and `<arch>` is e.g. `x86`:
  <https://developer.android.com/studio/run/emulator-commandline> ("Start the emulator
  from the command line" → system image directory description).
- The same path is the SDK package id with `;` → `/`:
  `system-images;android-36;google_apis_playstore;x86_64` ⇒
  `<sdk>/system-images/android-36/google_apis_playstore/x86_64/`. **VERIFIED** by the
  package ids published in Google's system-image repository XML
  (`https://dl.google.com/android/repository/sys-img/google_apis_playstore/sys-img2-3.xml`,
  and the sibling `google_apis`, `aosp_atd`, `google_atd`, `android` XML files) and by
  `avdmanager`/`-k` examples
  (<https://developer.android.com/studio/command-line/avdmanager>).
- **API directory names are not always plain integers.** The live Google repository
  publishes package ids such as `system-images;android-36.1;...`,
  `system-images;android-37.0;...`, `system-images;android-37.2-beta1;...`,
  `system-images;android-CANARY;...`. The directory segment is the raw API string after
  `android-` (`android-36.1`, `android-37.0`, …). Any parser that does `int(segment)` will
  break. **VERIFIED** (live XML, 2026-11-26). Note our `contracts` currently model
  `api_level: int` (`SystemImage.api_level`, `OobeRecord.api_level`); a minor API level
  cannot be represented exactly. Recommendation to root: keep `api_level` as the major
  int for UI/filtering and store the full SDK API string in `SystemImage.metadata`
  (e.g. `metadata["sdk_api"] = "36.1"`, `metadata["package"] = ...`); do not silently
  truncate. (Gap, not a blocker for custom images, which we synthesize ourselves.)

### 1.1 Files actually shipped in a real Google system-image archive

All names below verified from the zip central directories of the archives listed in
column "archive" (Google `dl.google.com`, 2026-11-26). Every archive has exactly one
top-level directory named after the ABI (`x86_64/`, `arm64-v8a/`, `x86/`); that top
directory is what lands at the `<abi>/` segment when installed by SDK tooling.

| File (inside `<abi>/`) | API 24 play x86 | API 28 play x86_64 | API 35 google_apis x86_64 | API 36 play x86_64 | Emulator needs it? |
|---|---|---|---|---|---|
| `system.img` | yes | yes | yes | yes | **required** |
| `vendor.img` | — | yes | yes | yes | optional (Treble/O+) |
| `ramdisk.img` | yes | yes | yes | yes | required |
| `kernel-ranchu` | yes | yes | yes | yes | **required** (QEMU2 path) |
| `userdata.img` | yes | yes | — | — | optional (seed for data partition) |
| `encryptionkey.img` | yes | yes | yes | yes | optional (encryption key partition) |
| `build.prop` | yes | yes | yes | yes | metadata; see §1.3 |
| `source.properties` | yes | yes | yes | yes | SDK package metadata; see §1.2 |
| `advancedFeatures.ini` | yes | yes | yes | yes | emulator feature overrides; see §1.4 |
| `kernel_cmdline.txt` | — | — | yes | yes | kernel cmdline file |
| `VerifiedBootParams.textproto` | — | yes | yes | yes | AVB/dm-verity params |
| `NOTICE.txt` | yes | yes | yes | yes | license attribution |
| `data/…` | yes | yes | yes | yes | misc device config (modem/skin/pixel-fold state, `empty_data_disk`) |
| `package.xml` | **no** | **no** | **no** | **no** | not shipped; SDK manager writes it on install |

Archives inspected (package → archive, sized from HEAD):
`system-images;android-36;google_apis_playstore;x86_64` → `x86_64-36_r07.zip`
(1,924,689,206 B, 42 entries), `…;arm64-v8a` → `arm64-v8a-36_r07.zip`,
`system-images;android-28;google_apis_playstore;x86_64` → `x86_64-28_r08.zip`,
`system-images;android-24;google_apis_playstore;x86` → `x86-24_r19.zip`,
`system-images;android-35;google_apis;x86_64` → `x86_64-35_r09.zip`,
`system-images;android-35;aosp_atd;x86_64` → `x86_64-35_r01.zip`.
**VERIFIED** (central-directory listing).

Notable consequences:

- A modern image is **not** just six files; `data/`, `advancedFeatures.ini`,
  `kernel_cmdline.txt`, `VerifiedBootParams.textproto`, `encryptionkey.img` and
  `NOTICE.txt` must be preserved. Import must copy the whole directory, not a
  hand-picked set.
- Brand-new API 35/36 images have **no `userdata.img`**. Emulator docs still describe
  `userdata.img` as the seed for `userdata-qemu.img`
  (<https://developer.android.com/studio/run/emulator-commandline> Table 1/Table 2), and
  API 24/28 images do ship it. Therefore the importer must treat `userdata.img` as
  optional and not fail validation on its absence (API 35/36 are verified absent). These
  images do ship `data/empty_data_disk`, which is consistent with the emulator
  synthesizing/blanking the data partition.
- `kernel-qemu` (old QEMU1 engine) is absent from every modern archive; only
  `kernel-ranchu` is shipped. Old images (API ≤ 24 pre-QEMU2) may be kernel-qemu based;
  the importer must accept either name and record which engine applies.

### 1.2 `source.properties` — the metadata file we can trust

**VERIFIED** content (extracted verbatim from `x86_64/source.properties` inside
`x86_64-36_r07.zip`, and equivalent files for API 35 google_apis and API 28 play):

```
Pkg.Desc=System Image x86_64 with Google Play.
Pkg.Revision=7
Pkg.Dependencies=emulator#35.4.9
AndroidVersion.ApiLevel=36
AndroidVersion.ExtensionLevel=17
AndroidVersion.IsBaseSdk=true
SystemImage.Abi=x86_64
SystemImage.TagId=google_apis_playstore
SystemImage.TagDisplay=Google Play
SystemImage.GpuSupport=true
Addon.VendorId=google
Addon.VendorDisplay=Google Inc.
```

API 35 `google_apis` additionally shows the same shape without `AndroidVersion.ExtensionLevel`
for older data, e.g. `Pkg.Dependencies=emulator#33.1.24`, `Pkg.Revision=9`; API 28 play has
`Pkg.Dependencies=emulator#27.1.7` and no `AndroidVersion.ExtensionLevel`/`IsBaseSdk`.
This file is the only machine-readable metadata guaranteed to be *inside* the archive:
use it as the primary source for api/abi/tag/vendor/min-emulator. It is OSS-style
`key=value` (no escaping; values may contain spaces).

### 1.3 `build.prop` (system-dir copy) — what it tells us

**VERIFIED** extracted keys from `x86_64/build.prop` in the API 36 play image:
`ro.build.version.sdk=36`, `ro.build.version.release=16`, `ro.build.type=user`,
`ro.build.tags=dev-keys`, `ro.product.cpu.abi=x86_64`, `ro.secure=1`,
`ro.debuggable=0`. API 35 `google_apis` has `ro.build.type=userdebug`,
`ro.debuggable=1`, same `ro.build.tags=dev-keys`. The emulator's boot code paths we
inspected do not open `<sysdir>/build.prop` (see §1.4); treat this file as
tooling/metadata (API/ABI/release cross-check), and **UNVERIFIED** whether Android Studio
or AVD Manager parses it for display. Do not require it for boot, but do cross-check it
against `source.properties` when present (mismatch = importer warning, possible wrong
directory).

### 1.4 What the emulator itself reads from the system directory

**VERIFIED** from Google's emulator docs (Table 1 "System directory files read by the
Android Emulator", emulator-commandline page):

| File | Meaning | CLI override |
|---|---|---|
| `kernel-ranchu` | binary kernel image | `-kernel <file>` |
| `ramdisk.img` | boot/ramdisk partition | `-ramdisk <file>` |
| `system.img` | read-only system partition | `-system <file>` |
| `userdata.img` | initial data partition (seed) | `-initdata <file>` |

Docs also say: "Use the `-sysdir` option to specify a different system directory for the
AVD." and give an example `emulator @Pixel8_API_34 -sysdir <image-dir>`. The same page
documents `-vendor`, `-datadir`, `-partition-size`, `-writable-system`
(<https://developer.android.com/studio/run/emulator-commandline>).

**VERIFIED** from AOSP emulator source (`external/qemu`, branch `emu-master-dev`,
`android/android-emu/android/main-common.c`):

- Kernel: when `-kernel` is not passed, `avdInfo_getRanchuKernelPath()` (QEMU2) or
  `avdInfo_getKernelPath()` resolves it; if none: error
  `This AVD's configuration is missing a kernel file! Please ensure the file
  "kernel-ranchu" is in the same location as your system image.` plus additional
  `ANDROID_SDK_ROOT is defined (...) but cannot find kernel file in
  <sdk>/system-images/ sub directories` / `ANDROID_SDK_ROOT is undefined` diagnostics;
  then `Invalid or missing kernel image file: %s`.
- Vendor: `-vendor` else `avdInfo_getVendorImagePath()` / `avdInfo_getVendorInitImagePath()`;
  source comment "Vendor image is for O+ only"; when nothing is found it prints
  `No vendor image` and continues (vendor is **optional**).
- System: `System image file doesn't exist: %s`; and later
  `Missing system image: %s` / partition-size checks.
- Encryption key: `-encryption-key` else `avdInfo_getEncryptionKeyImagePath()`.
- No `package.xml`, `source.properties`, `build.prop`, `advancedFeatures.ini` or
  `kernel_cmdline.txt` reads exist in `main-common.c` (searched). At least the *boot*
  path does not need package metadata; `advancedFeatures.ini` is consumed by the
  emulator's feature-flag system (filename present in every modern archive; exact reader
  file not traced — **PARTIAL**).

**Consequence for R-CUSTOM-IMAGE:** a directory containing the verified file set boots
without any SDK package registration, provided an AVD configuration points the emulator
at it. Registration only matters to `avdmanager`/Studio/SDK Manager, not to the
emulator's boot engine.

### 1.5 AVD `config.ini` / `.ini` fields relevant to custom images

- `image.sysdir.1=<path>` in `<avd>/config.ini` is how an AVD names its system directory.
  Installed images use the **relative** POSIX-ish path
  `system-images/android-<api>/<tag>/<abi>/`; the emulator resolves it against
  `ANDROID_SDK_ROOT`/`ANDROID_HOME` (this is why images can be relocated with the SDK).
  **PARTIAL**: path resolution is exercised by every real AVD and the emulator's kernel
  error message explicitly talks about `<sdk>/system-images/ sub directories`, but no
  public doc page documents the `config.ini` key itself; the value format is corroborated
  by `avdmanager`-generated AVDs (`tools/base` source: `image.sysdir.1` is populated from
  the selected `ISystemImage`) and by our `contracts/fakes-spec.md` (which fakes it).
- `<avd>.ini` (in `$ANDROID_AVD_HOME`) contains `path=<absolute dir of *.avd>`,
  `path.rel=avd/<name>.avd`, `target=android-<api>` and `avd.ini.encoding=UTF-8`;
  `avdmanager move avd -n <name> -p <path>` exists specifically to rewrite it
  (<https://developer.android.com/studio/command-line/avdmanager>). **PARTIAL** (docs
  prove the move command exists; exact key list is from generated AVDs, not a docs page).
- For a custom image, prefer a **relative** `image.sysdir.1` under the SDK root; if the
  image lives outside the SDK, `image.sysdir.1` must be absolute and becomes a relocation
  liability (see R7). The fallback that avoids editing `config.ini` entirely is
  `emulator -avd <name> -sysdir <dir>` at launch (**VERIFIED** flag).

---

## 2. How `avdmanager` / the emulator recognise images

### 2.1 Emulator — file-based, no registry

The emulator resolves an AVD by name from `$ANDROID_AVD_HOME`,
`$ANDROID_USER_HOME/avd/`, `$HOME/.android/avd/` (in that order; **VERIFIED**
<https://developer.android.com/tools/variables>), reads `<name>.ini` → `path=`, then reads
`config.ini`, and opens the files listed in §1.4 from the system dir. It does not consult
`package.xml` or the SDK repository for installed images. **VERIFIED** by source (§1.4)
and the documented `-sysdir` override.

### 2.2 `avdmanager` — package-based, and now deprecated

**VERIFIED** from `tools/base` source
(`sdklib/src/main/java/com/android/sdklib/tool/AvdManagerCli.java`, branch
`mirror-goog-studio-main`; GitHub-mirror line numbers below are from that file):

- `create avd` takes `-k <sdk_id>`; `mSdkHandler.getLocalPackage(packagePath, progress)`
  must return a `LocalPackage`. If it does not:
  `Package path is not valid. Valid system image paths are:` followed by the list of
  image paths (lines ~692–704).
- Then `mSdkHandler.getSystemImageManager(progress).getImagesInPackage(imagePkg)` must be
  non-empty, otherwise
  `Package <name> (<path>) contains no system images. Valid system image paths are:`.
- Tag/ABI must be consistent with the package's `SysImgDetailsType`; `--device` accepts a
  device-definition id.

That means a custom image that is **not** registered as a local SDK package cannot be
used through `avdmanager create avd -k` (it will print "Package path is not valid.").
This matches our fakes contract (`contracts/fakes-spec.md` §4.2). How exactly the local
package loader discovers packages (which files it parses) was not confirmed from source in
this research window (**UNVERIFIED**); evidence points to local package metadata under the
package dir (`source.properties`/`package.xml`). The importer therefore registers
*defensively* (§5, step S1) and verifies by running the tools, with a non-registered
fallback (§5, S2/S3).

**Product-relevant change (VERIFIED, docs last updated 2026-09-02):** `avdmanager` is now
deprecated in favour of the Android CLI:

> "The `avdmanager` tool is deprecated. Instead, use the Android CLI `android emulator`
> command to create AVDs. `android emulator [create|remove]`"
> — <https://developer.android.com/studio/command-line/avdmanager>

S1a (Android CLI backend) must become the primary AVD-creation path, with
`avdmanager` as the legacy backend (S1b), and the fakes contract already models both.
Whether `android emulator create` accepts custom local images the same way is
**UNVERIFIED**; assume the same package-based validation until tested on a real host.

---

## 3. Studio SDK update sites / repository XML

### 3.1 The supported way to publish a custom image

**VERIFIED** (Android Studio docs, "Edit or add SDK tool sites"):

- Android Studio → Settings → Appearance & Behavior → System Settings → Android SDK →
  **SDK Update Sites** tab → **Add** a site URL. "Android Studio checks for Android tools
  and third-party tool updates from the SDK sites you add."
  <https://developer.android.com/studio/intro/update#sdk-update-sites>
- The site is a URL to a repository XML (add-on site / sys-img site), which lists packages
  with `<archive><complete/></archive>` blocks pointing at downloadable zips.
- AOSP documents exactly this flow for custom AVD images: host the image zip, host
  `repo-sys-img.xml`, add the XML URL as a Custom Update Site, then the image appears on
  the **System Images** page and can be downloaded/selected to create an AVD.
  <https://source.android.com/docs/setup/test/avd> ("Share AVD system images for others to
  use with Android Studio", steps: "Add the Custom Update Site URL as an SDK Update Site.
  This adds your custom AVD system image to the System Images page. Create an AVD by
  downloading and selecting the custom AVD system image.")

Caveat: `sdkmanager` (command line) has **no documented flag to add a custom update
site** — only Studio's UI does. The AOSP docs' flow is Studio-centric. Therefore
avdfleet's local-directory import cannot rely on "add a site and install"; it needs §5
S1/S2/S3, and optionally can *generate* an update-site bundle (zip + sys-img XML) for
users who want Studio to see the image. **VERIFIED** for the absence of a documented
`sdkmanager --add-site` (search of
<https://developer.android.com/studio/command-line/sdkmanager>: only `--sdk_root`,
`--list`, `--install`, `--uninstall`, `--licenses`, `--channel`, `--verbose`,
`--include_obsolete`, `--proxy` documented; the custom-site capability is Studio-only).

### 3.2 Exact repository XML dialects (three, don't mix them)

**VERIFIED** by fetching live XML (2026-11-26) and AOSP schema files:

| XML | Root element / namespace | Serves |
|---|---|---|
| `repository2-3.xml` (`<sdk-repository>`) | `xmlns:sdk="http://schemas.android.com/sdk/android/repo/repository2/03"` | tools, platforms, build-tools, emulator, platform-tools |
| `sys-img2-3.xml` (`<sdk-sys-img>`) | `xmlns:sys-img="http://schemas.android.com/sdk/android/repo/sys-img2/03"` | system images (what we need) |
| `addon2-3.xml` (`<sdk-addon>`) | add-on schema | add-ons (vendor libs) |
| legacy `sdk-sys-img-03.xsd` (`<sdk-sys-img>` ns `http://schemas.android.com/sdk/android/sys-img/3`) | old schema still shipped in AOSP `prebuilts/devtools/repository/sdk-sys-img-01..03.xsd`; used by AOSP-generated `repo-sys-img.xml` for Android 12 and lower | older custom sites |

Live sys-img package entry (exactly as served by Google; a working template for a custom
update site) — **VERIFIED** from
`https://dl.google.com/android/repository/sys-img/google_apis_playstore/sys-img2-3.xml`:

```xml
<remotePackage path="system-images;android-36;google_apis_playstore;x86_64">
  <type-details xsi:type="sys-img:sysImgDetailsType">
    <api-level>36</api-level>
    <extension-level>17</extension-level>
    <base-extension>true</base-extension>
    <tag><id>google_apis_playstore</id><display>Google Play</display></tag>
    <vendor><id>google</id><display>Google Inc.</display></vendor>
    <abi>x86_64</abi>
  </type-details>
  <revision><major>7</major></revision>
  <display-name>Google Play Intel x86_64 Atom System Image</display-name>
  <uses-license ref="android-sdk-license"/>
  <dependencies>
    <dependency path="emulator">
      <min-revision><major>35</major><minor>4</minor><micro>9</micro></min-revision>
    </dependency>
  </dependencies>
  <channelRef ref="channel-0"/>
  <archives>
    <archive>
      <complete>
        <size>1924689206</size>
        <checksum type="sha1">16fa3c441d29fde6c9eea2f766eecf77032d68b4</checksum>
        <url>x86_64-36_r07.zip</url>
      </complete>
    </archive>
  </archives>
</remotePackage>
```

Facts worth carrying into G1x/R1:

- Archive URLs are **relative to the XML's directory** (`x86_64-36_r07.zip`); Google does
  not publish SHA-256 in this XML — only **SHA-1** (`checksum type="sha1"`) plus size.
  R1's SHA-256 catalogue values must come from our own download-and-hash pass or a
  separate channel; do not claim Google publishes SHA-256. **VERIFIED**.
- `Pkg.Dependencies=emulator#35.4.9` in `source.properties` mirrors the XML
  `<dependency path="emulator"><min-revision>…` — an image can require a newer emulator
  than installed. Importer/doctor must check this. **VERIFIED**.
- `channelRef ref="channel-0"` = stable channel. **VERIFIED**.
- The XML's license text is embedded (`<license id="android-sdk-license" type="text">`),
  so a custom site must also carry a license element for SDK Manager to accept packages.
  **VERIFIED** (license element present in fetched XML; SDK Manager license handling).
- Tag ids seen in live data: `google_apis_playstore`, `google_apis_playstore_ps16k`
  (16 KB page-size variants), `google_apis`, `aosp_atd`, `google_atd`, `default`
  (AOSP images; `system-images;android-35;default;x86_64` etc. come from
  `.../sys-img/android/sys-img2-3.xml`). **VERIFIED**.
- AOSP's own `make sdk_repo` emits `repo-sys-img.xml` using the **legacy**
  `sdk-sys-img-03.xsd` schema for Android 12 and lower (AOSP
  `development/build/tools/sdk_repo.mk` selects the newest
  `prebuilts/devtools/repository/sdk-sys-img-*.xsd`; `mk_sdk_repo_xml.sh` is the
  generator). Whether current Studio still accepts the legacy sys-img dialect is
  **UNVERIFIED**; for our generated site, mirror the live `sys-img2/03` XML instead.

---

## 4. AOSP `emu_img_zip` build artifacts

**VERIFIED** from AOSP docs (<https://source.android.com/docs/setup/test/avd>, accessed
2026-11-26):

- Build an emulator system image: `source build/envsetup.sh`, `lunch sdk_phone_x86_64`,
  `make -j32` (same process as a device system image), then `emulator` runs it from the
  build tree.
- To share it with Android Studio: for **Android 13 and higher** run `make emu_img_zip`,
  which "generates an `sdk-repo-linux-system-images-eng.[username]].zip` file." For
  **Android 12 and lower** run `make -j32 sdk sdk_repo`, which creates under
  `out/host/linux-x86/sdk/sdk_phone_x86/` both `sdk-repo-linux-system-images-eng.[username].zip`
  and `repo-sys-img.xml`.
- Then: host the zip, set its URL as the "AVD System Image URL"; for Android 12 and
  lower edit `<sdk:url>` in `repo-sys-img.xml` (schema `sdk-sys-img-03.xsd`), host the
  XML, get its URL as the "Custom Update Site URL"; in SDK Manager add that URL as an SDK
  Update Site; the image appears on the System Images page; create an AVD by downloading
  and selecting it.

**Are the artifacts directly usable as an avdfleet custom-image directory?** **No, not
directly** (VERIFIED conclusion from the documented workflow + §2):

1. The zip is an *sdk-repo archive* meant to be served from an update site; it is not a
   directory `avdmanager` can reference. Using it as-is still requires the custom-site
   download flow (Studio UI) or manual extraction into
   `<sdk>/system-images/android-<api>/<tag>/<abi>/` plus registration.
2. The documented AOSP path produces an XML + zip pair; the XML is required for Studio.
3. These are AOSP/engineering or userdebug builds (`ro.build.tags=dev-keys`,
   `ro.build.type=userdebug` in the stock-Google analogue; AOSP builds are not
   Play-certified and contain no Play Store). They are not Google Play images and must
   never be labelled as such.
4. The internal archive layout of an `emu_img_zip` artifact is **UNVERIFIED**: we could
   not find the target definition in AOSP within the research window (it is not in
   `build/make/core/Makefile`, `build/make/core/tasks/sdk-addon.mk` or
   `development/build/tools/sdk_repo.mk` on `main`; docs only name the output). The
   nearby AOSP `sdk_addon` packaging (VERIFIED,
   `build/make/core/tasks/sdk-addon.mk`) stages
   `images/<TARGET_CPU_ABI>/{system.img,vendor.img,ramdisk.img,build.prop,userdata.img,NOTICE.txt}`
   plus `images/source.properties` and zips that, i.e. an `images/<abi>/…` layout that
   differs from Google's `<abi>/…` layout. **Therefore the importer must not assume one
   layout: detect both `<abi>/` and `images/<abi>/` (and a plain directory) and normalize
   by locating the directory that contains `system.img`.**
5. For Android 13+ `emu_img_zip`, the docs say `repo-sys-img.xml` is generated only by
   the Android-12-and-lower `sdk sdk_repo` path ("For Android 12 and lower, edit
   repo-sys-img.xml"); whether 13+ also emits it is unclear — **UNVERIFIED**. If it does
   not, a custom site needs a hand-written sys-img XML (use the §3.2 template).

Practical recommendation: avdfleet should accept an `emu_img_zip` as an input artifact
and either (a) unzip + normalize + register locally via §5, or (b) generate a custom
update-site bundle (normalized zip + `sys-img2/03` XML with relative `<url>` and
`<size>`/`<checksum>` computed by us) for users who want Studio to consume it.

---

## 5. Recommended import algorithm for avdfleet (concrete)

Implements `ImageCatalog.import_directory()` (contracts/openapi.yaml
`POST /api/v1/images` → `ImageImportRequest{path,name,image_id,api_level,abi}`). The
method name says directory; support `.zip` too (a zip path is a file, so either add a
`kind` field or detect by extension — API change decision for root).

### 5.0 Inputs and invariants

- Inputs: `path` (dir or `.zip`), optional `name`, `image_id`, `api_level`, `abi`.
- All writes go under `Paths.sdk_root / "system-images"` plus our data dir; never modify
  the source artifact; source may be read-only media.
- The operation must be **idempotent**: re-importing the same content returns the same
  `SystemImage.id` and changes nothing (`installed=true`, same checksum).
- Dry-run returns the full plan (target paths, bytes, validations, registration actions)
  and touches nothing. Every destructive step (overwrite) requires `--force`.

### 5.1 Phase A — preflight (no writes)

1. `path` exists; readable; reject if it resolves inside `Paths.sdk_root` in a place that
   would make our own state circular (allowed if it is already a managed image → detect
   manifest).
2. If directory: walk without following symlinks; reject symlinks/reparse points anywhere
   in the tree (portable edition target FS may not support them; AVD/emulator do not need
   them). Reject paths with `..`, absolute entries, or > 4096 entries.
3. If zip: open central directory only; reject absolute/`..` names, symlink entries
   (Unix mode `S_IFLNK`), encrypted entries, total uncompressed > `max_import_bytes`
   (default 8 GiB), compression ratio > 200:1 per entry, duplicate names.
4. Compute `total_bytes` (sum of sizes) and free space on the target FS; require
   `free >= total_bytes * 1.1 + 1 GiB`. Reject FAT32 (R7) — it cannot hold >4 GiB files.
5. Reject Windows reserved names/drive roots, paths ≥ 240 characters, and target
   `system-images/android-<api>/<tag>/<abi>` components not matching
   `^[A-Za-z0-9._-]+$`.

### 5.2 Phase B — normalize the layout

Locate the **image root**: the directory that directly contains `system.img`. Accepted
shapes:

- `dir/` (image root == input path);
- `dir/<abi>/…` (Google sdk-repo archive extracted, or an SDK system-images tree);
- `dir/images/<abi>/…` (AOSP `sdk_addon`/legacy layout);
- `dir/system-images/android-<api>/<tag>/<abi>/…` (a whole `<sdk>` subtree — allow only
  one image selection; if multiple, require `--name`/`image_id` to pick).
- zip: same, after browsing the central directory; extraction to a temp dir inside
  `Paths.cache_dir` (same FS as target), then rename into place (atomic, same-volume).

`abi` = the directory name found at the level above the image root when present;
otherwise read `SystemImage.Abi` from `source.properties`, else infer from
`build.prop` `ro.product.cpu.abi`, else require `--abi`.

### 5.3 Phase C — read and cross-check metadata

1. Parse `source.properties` if present: `AndroidVersion.ApiLevel` (string, may be
   `36.1`), `AndroidVersion.ExtensionLevel`, `SystemImage.Abi`, `SystemImage.TagId`,
   `SystemImage.TagDisplay`, `Addon.VendorId`, `Pkg.Revision`, `Pkg.Dependencies`.
2. Parse `build.prop`: `ro.build.version.sdk`, `ro.product.cpu.abi`,
   `ro.build.version.release`, `ro.build.type`, `ro.debuggable`.
3. Resolve final values with precedence: explicit CLI/API override > `source.properties`
   > `build.prop` > directory name; **every disagreement is a validation warning**, and
   two disagreeing authoritative sources (source.properties vs build.prop on sdk/abi) is
   a **hard error** unless the user overrides both.
4. Derive `tag` (default `custom`) and validate tag regex; derive `display_name`
   (`name` or `Custom <api> <tag> <abi>`).
5. Read `advancedFeatures.ini`; if `PlayStoreImage = on`, record
   `metadata["advanced_features"]["PlayStoreImage"]=True` and warn "Play-like image;
   avdfleet will not root/modify it or bypass Play protections" (BRIEF §8 out-of-scope).
6. Compute the stable id: `image_id or "custom-" + slugify(f"{tag}-{abi}-android{api}")`;
   collision on id with different content ⇒ error (409 in API terms) unless `--force`.
   Store `metadata.sdk_package = "system-images;android-<api>;<tag>;<abi>"`,
   `metadata.source_path`, `metadata.source_sha256` (archive sha256 if zip),
   `metadata.files_sha256` map, `metadata.source_properties` (raw dict),
   `metadata.build_prop` (subset copied above), `metadata.layout` (`dir|abi|images-abi|sdk`),
   `metadata.engine` (`ranchu|qemu1` depending on kernel filename).

### 5.4 Phase D — validation rules (must all pass unless noted WARN)

| # | Rule | Failure |
|---|---|---|
| V1 | `system.img` exists, regular file, size ≥ 8 MiB, readable | FAIL |
| V2 | `ramdisk.img` exists, size ≥ 64 KiB | FAIL |
| V3 | `kernel-ranchu` exists OR `kernel-qemu` exists (record which; prefer ranchu when both) | FAIL |
| V4 | `vendor.img`: if API ≥ 27 and absent → WARN (may still boot if system embeds vendor); if present, size ≥ 1 MiB | WARN/FAIL |
| V5 | `userdata.img` optional; if present size ≥ 1 MiB | WARN |
| V6 | API parses from metadata, is `>= 21` (our `SUPPORTED_API_RANGE` start) and matches `<api>` dir / `android-<api>` sdk dir; extension images keep `ext` suffix | FAIL/WARN |
| V7 | ABI in `{x86, x86_64, arm64-v8a, armeabi-v7a}`; host-compatible: x86_64 host accepts x86/x86_64; arm64 host accepts arm64-v8a; **Apple Silicon cannot run x86/x86_64 emulator images** (no Intel emulation in the emulator) | FAIL |
| V8 | `source.properties` Pkg.Dependencies min emulator ≤ installed emulator version | FAIL (doctor remedy) |
| V9 | `build.prop` `ro.build.version.sdk` == resolved API, `ro.product.cpu.abi` == ABI | FAIL on mismatch |
| V10 | `system.img` header readable (sparse magic `0x3AFF26ED` little-endian vs ext4 superblock `0xEF53` at offset 1080); unknown format → WARN (may still boot) | WARN |
| V11 | `advancedFeatures.ini` PlayStoreImage=on + tag claims playstore → WARN + never auto-label `oobe verified` | WARN |
| V12 | Any Play Store claim on a custom image is not accepted as a Play image (`kind=custom`, `metadata.play_like=true`); no attempt to modify/root it | n/a (policy) |
| V13 | `NOTICE.txt` present → copy for attribution; absent → WARN | WARN |
| V14 | No file inside the tree exceeds target FS per-file limit (FAT32 4 GiB excluded by preflight); no total > free space | FAIL |
| V15 | sha256 computed for every file; manifest written to our data dir; re-import identical ⇒ no-op | FAIL on read error |

If all FAILs pass, continue.

### 5.5 Phase E — install (registration strategy, ordered, all verified after each step)

**Target dir:** `<sdk_root>/system-images/android-<api>/<tag>/<abi>/` (the abi segment
comes from the archive's top dir; strip it when copying the normalised image root).

**S1 — managed import (preferred).**
1. Copy normalised tree into a temp dir next to target, fsync, then `os.replace` into
   place (rollback on error). Mark files read-only only where emulator requires none —
   do not chmod; write under user's own SDK.
2. Write/merge `source.properties` at `<target>/source.properties` with the exact keys
   from §1.2 (we synthesize api/abi/tag/vendor/revision; never lose imported values).
3. Write a local `package.xml` describing the package (sdlk local package: path
   `system-images;android-<api>;<tag>;<abi>`, type-details api-level/tag/abi, revision,
   display-name). **UNVERIFIED** exact local `package.xml` schema; treat this as a
   best-effort registration and verify (next step). Do not corrupt pre-existing
   `package.xml`; back it up as `package.xml.avdfleet-bak`.
4. Verify registration:
   `sdkmanager --sdk_root=<sdk> --list_installed` must show the package path, and
   `avdmanager create avd -n <probe> -k "system-images;android-<api>;<tag>;<abi>"
   --device pixel_7 --force` must exit 0. If both pass → S1 done.
5. If verification fails, roll back `package.xml`/`source.properties` to pre-import
   state (keep image files), log warning, continue to S2.

**S2 — AVD skeleton reuse (fallback, works without registration).**
1. Pick any *installed* stock image with the same ABI (and same/close API) from the
   catalogue; `avdmanager create avd -n <avd> -k <stock_pkg> --device <device> --force`.
2. Rewrite `<avd>/config.ini` keys atomically:
   `image.sysdir.1=system-images/android-<api>/<tag>/<abi>/` (relative, forward slashes),
   `tag.id=<tag>`, `abi.type=<abi>`, `PlayStore.enabled=false` (custom/unknown),
   `image.sysdir.2` removed if present.
3. Delete stale `hardware-qemu.ini`, `*.lock`, `snapshots/*` for the new AVD.
4. This is the primary fallback because it produces a fully avdmanager-generated AVD
   (correct hardware config/skin/device profile) and then only changes the image dir.

**S3 — manual AVD files (last resort, e.g. no stock image installed).**
Write `<avd>.ini` and `<avd>/config.ini` ourselves with the minimum:
`avd.ini.encoding=UTF-8`, `path=<abs .avd dir>`, `path.rel=avd/<name>.avd`,
`target=android-<api>`; config: `AvdId`, `avd.ini.displayname`, `avd.ini.encoding`,
`image.sysdir.1`, `tag.id`, `abi.type`, `hw.device.name`, `hw.ramSize`,
`hw.cpu.ncore`, `disk.dataPartition.size`, `hw.keyboard`, `fastboot.forceColdBoot=yes`,
`PlayStore.enabled=false`. Validate by launching once (`-no-window -no-snapshot`).
**PARTIAL** — no public doc page defines these files; our `contracts/fakes-spec.md` and
real avdmanager output converge on this set, but treat S3 as reliability fallback only.

**S4 — runtime sysdir override (no AVD edit).**
If a launch must be tried without touching AVD files at all:
`emulator -avd <name> -sysdir <abs image dir> -no-window -no-snapshot -no-audio …`
(**VERIFIED** flag exists; docs say it sets the system directory for the AVD). Keep this
only for probes; the AVD record must still be resolved for cleanup/snapshots.

**Record** in our store: image (`SystemImage.custom=True, kind=custom, local_path=target,
installed=True, oobe_status=unverified`), registration outcome
(`metadata.registration = "s1_verified" | "s1_failed_s2" | "s3_manual"`), and for S1 the
`package.xml` backup path.

### 5.6 Phase F — create-and-boot validation (the acceptance test for R-CUSTOM-IMAGE)

1. Create/reuse AVD from the imported image (`S1` → avdmanager `-k`; else `S2`).
2. Launch headless with camera/mic/audio off, `-no-snapshot` for the first boot,
   `-gpu swiftshader_indirect` fallback, unique `-port`, proxy flags per instance.
3. Poll `adb -s emulator-<port> shell getprop sys.boot_completed` until `1`
   (boot_timeout_s). Then assert:
   - `getprop ro.build.version.sdk` == resolved API,
   - `getprop ro.product.cpu.abi` == resolved ABI,
   - `getprop ro.kernel.qemu` == `1` (we are in an emulator),
   - package manager ready (`adb shell pm path android` succeeds).
4. Only then set `installed=true` + emit `image.imported`; record boot time and OOBE
   detection placeholder (`oobe_status=unverified` until R3/R-OOBE flow runs).
5. On failure keep the emulator log (`-verbose -show-kernel`, `logcat`, `adb devices`),
   mark the image `failed` with a machine-readable `failure_mode` from §5.7, and leave
   files in place for diagnosis unless `--clean-on-failure`.

### 5.7 Failure modes (explicit, actionable)

| Failure | Detection | User-visible error (envelope) | Remedy |
|---|---|---|---|
| Not an image layout | `system.img` not found after normalisation | `custom_image_layout_unknown` | Ask user to point at the directory containing system.img |
| Missing required file | V1–V3 | `custom_image_incomplete` (list files) | Re-export/re-download |
| Kernel/engine mismatch | boot: `missing a kernel file … kernel-ranchu` | `custom_image_kernel_missing` | Add kernel or use image built for QEMU2 |
| API/ABI metadata conflict | §5.3 step 3 | `custom_image_metadata_conflict` (all values) | Override api/abi explicitly |
| Unsupported ABI on host | V7 | `custom_image_abi_unsupported` | Use host-arch image (arm64 on Apple Silicon) |
| Emulator too old | V8 | `custom_image_requires_emulator` | `avdfleet doctor --fix` / upgrade emulator |
| Package not recognised | S1 verification | `custom_image_registration_failed` → S2 | none needed, fallback used |
| AVD name exists | avdmanager exit 1 / our precheck | `avd_exists` | `--force`/new name |
| Windows path too long / reserved | preflight | `custom_image_path_invalid` | Shorten SDK root |
| FS too small / FAT32 / >4 GiB | preflight | `custom_image_filesystem_unsupported` | Move SDK/AVD dir, use NTFS/APFS/ext4 |
| Zip unsafe (traversal/symlink/bomb) | preflight | `custom_image_archive_unsafe` | Rebuild archive |
| Corruption/short zip | central dir/sha mismatch | `custom_image_archive_corrupt` | Re-download |
| AVB/dm-verity boot failure | boot timeout, `-show-kernel` shows `dm-verity` | `custom_image_boot_avb` | Use image with matching VerifiedBootParams/kernel |
| Boot loop/timeout | boot timeout | `custom_image_boot_timeout` (+log path) | Check log; try `-wipe-data`/different device profile |
| Locked files (AV/Defender) | copy EACCES/EBUSY | `custom_image_copy_failed` (+retry count) | Retry, add exclusion |
| Disk full mid-copy | ENOSPC | `custom_image_disk_full` | Rollback, free space |
| Play Store expectations | user selects a Play tag for custom image | `custom_image_not_play_certified` (warning, not fatal) | Use stock `google_apis_playstore` via sdkmanager |

### 5.8 Security / product constraints

- Custom images are untrusted input. Never mount/extract with elevated privileges, never
  execute code from the image, never auto-accept licenses on the user's behalf, and never
  modify the image (out-of-scope root/Play tampering per BRIEF §8).
- The importer is the only component that writes under `system-images/`, and it must
  record what it wrote (manifest) so `avdfleet doctor`/uninstall can clean up.
- `PlayStore.enabled=true` must only ever be set for stock Google Play packages obtained
  through the SDK manager path (they are signed with release keys and not rootable —
  **VERIFIED** <https://developer.android.com/studio/run/managing-avds>: "system images
  with the Google Play Store included are signed with a release key, which means that you
  can't get elevated privileges (root) with these images"). For custom images: false.

---

## 6. Play Store image constraints (what the importer/catalogue must respect)

- **Tags/ABIs/API levels (VERIFIED, live `sys-img2-3.xml` 2026-11-26).** Play images exist
  only under tags `google_apis_playstore` and `google_apis_playstore_ps16k` (16 KB page
  size). ABIs: `x86` (API 24–30), `x86_64` and `arm64-v8a` (API 28–37.x; **x86_64/arm64
  Play images start at API 28**). API ids include `android-36.1`, `android-37.0`,
  `android-37.1`, `android-37.2`, `-beta*`, `android-CANARY`, and `-extNN` extension
  levels. No `armeabi-v7a`/`mips` Play images.
- **No root / not writable (VERIFIED)**: release-key signed; `adb root` unavailable. AOSP
  rootable images (tag `default`, `aosp_atd`) are the supported alternative
  (<https://developer.android.com/studio/run/managing-avds>). `-writable-system` requires
  `adb root`/`adb remount` (VERIFIED docs for `-writable-system`), so it is not usable on
  Play images.
- **License click-through (VERIFIED)**: package entries carry
  `<uses-license ref="android-sdk-license"/>`; license text is embedded in the repo XML;
  `sdkmanager --licenses` must have accepted it. avdfleet only accepts licenses with the
  explicit `--accept-android-sdk-licenses` flag (BRIEF R-INSTALL).
- **Redistribution**: the SDK license covers downloading via the SDK channel; bundling
  Google system images in a redistributed portable bundle is a legal question — treat as
  **NOT ALLOWED** for v0.1 (no redistribution of Google images), and have
  `bundle --offline` cache only what the user obtained/prompted (PLAN decision 5 + R7).
- **Play Store needs a Google account and network at runtime**; avdfleet explicitly does
  not automate account creation (BRIEF §8) and must not modify Play images.
- **Play-like heuristics for stock detection**: stock Play images have
  `advancedFeatures.ini` `PlayStoreImage = on` (VERIFIED) and `build.prop`
  `ro.build.type=user`, `ro.debuggable=0`, `ro.build.tags=dev-keys` (VERIFIED API 36);
  Google APIs (non-Play) images have `ro.build.type=userdebug`, `ro.debuggable=1`
  (VERIFIED API 35). Use only as a *signal*, never as proof; the only proof of Play
  certification is provenance (package downloaded from `google_apis_playstore` site).

## 7. Cross-team implications (short)

- **G1x (`platform/images/`)**: implement §5; add zip support and layout detection; store
  `source.properties`-derived metadata; generate stable custom ids; never publish SHA-256
  as "from Google" (Google's XML has SHA-1 only). Catalogue must parse minor API ids
  (`android-36.1`, `android-37.0`) without int().
- **A1 (`platform/avd/`)**: `config.ini` editing must know `image.sysdir.1`, `tag.id`,
  `abi.type`, `PlayStore.enabled`, and must support creating an AVD from a *custom* image
  via S2 (stock skeleton + sysdir rewrite). `avdmanager move avd` is documented for
  relocation (R7).
- **S1a/S1b (`platform/sdk/`)**: `android` CLI is now primary (`avdmanager` deprecated);
  `sdkmanager --list_installed` is the registration oracle; `--sdk_root` everywhere.
- **N1 (`platform/runtime/`)**: launch must pass `-sysdir` only for probes; real launches
  use the AVD's `image.sysdir.1`; surface the documented emulator errors from §1.4
  verbatim in logs (useful diagnostics).
- **PT3 (`portable/relocate/`)**: custom-image AVDs with absolute `image.sysdir.1` break on
  relocation — write relative paths and document exceptions (see R7).
- **Contract gap**: `ImageImportRequest`/`SystemImage` have no field for zip vs directory,
  no `api` string (minor levels), no `registration`/`layout` metadata. Keep in
  `metadata: dict` for v0.1; flag to root for v0.2.

## 8. UNVERIFIED / must-test-on-accelerated-host list

1. Exact local `package.xml` format accepted by current `sdkmanager`/`avdmanager`/Android
   CLI for a hand-written package registration (S1). Verify with a real SDK on E0/host.
2. Whether `android emulator create` (new Android CLI) validates custom images the same
   way `avdmanager -k` does.
3. Exact internal layout of AOSP `emu_img_zip` output (target definition not found); verify
   by downloading one artifact on a build-capable host and listing its central directory.
4. Whether current Studio accepts the legacy `sdk-sys-img-03` dialect for custom update
   sites (AOSP ≤12 generator output).
5. Whether `build.prop`/`source.properties` are consumed by Studio's AVD Manager for
   display/metadata (emulator boot path does not read them).
6. Real boot proof for every claimed layout (this host has no hypervisor; see PLAN.md).
7. `advancedFeatures.ini` parsing location/behavior for feature flags (filename is standard
   in all archives).

## 9. Sources (all accessed 2026-11-26)

Primary docs / Google:

- Emulator command-line (system directory files, `-sysdir`, `-kernel`, `-ramdisk`,
  `-system`, `-initdata`, `-datadir`, `-vendor`, `-partition-size`, `-writable-system`):
  <https://developer.android.com/studio/run/emulator-commandline>
- AVD manager (system images, Play release-key/no-root, hardware profiles import/export):
  <https://developer.android.com/studio/run/managing-avds>
- `avdmanager` (deprecated in favour of Android CLI; `-k`, `move avd`):
  <https://developer.android.com/studio/command-line/avdmanager>
- `sdkmanager` (`--sdk_root`, `--list`, `--install`, `--licenses`; no custom-site flag):
  <https://developer.android.com/studio/command-line/sdkmanager>
- Environment variables (`ANDROID_AVD_HOME`, `ANDROID_USER_HOME`,
  `ANDROID_EMULATOR_HOME`): <https://developer.android.com/tools/variables>
- Android Studio SDK Update Sites / "Edit or add SDK tool sites", SDK Manager:
  <https://developer.android.com/studio/intro/update>
- AOSP "Use Android Emulator virtual devices" (custom image sharing workflow, `emu_img_zip`,
  `sdk_repo`, `repo-sys-img.xml`, Custom Update Site steps):
  <https://source.android.com/docs/setup/test/avd>

Google repository XML (fetched live; also used for package ids, tags, ABIs, API strings,
SHA-1/size semantics):

- <https://dl.google.com/android/repository/sys-img/google_apis_playstore/sys-img2-3.xml>
- <https://dl.google.com/android/repository/sys-img/google_apis/sys-img2-3.xml>
- <https://dl.google.com/android/repository/sys-img/aosp_atd/sys-img2-3.xml>
- <https://dl.google.com/android/repository/sys-img/google_atd/sys-img2-3.xml>
- <https://dl.google.com/android/repository/sys-img/android/sys-img2-3.xml> (AOSP `default` tags)
- <https://dl.google.com/android/repository/repository2-3.xml> (root namespaces/dialect)

AOSP source (read via gitiles `?format=TEXT`; repo `platform/...`, branch noted):

- Emulator image-file resolution, kernel/vendor/system errors, `-sysdir`:
  `external/qemu` @ `emu-master-dev`, `android/android-emu/android/main-common.c`
  (<https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-master-dev/android/android-emu/android/main-common.c>)
- `avdmanager create avd` package validation:
  `tools/base` @ `mirror-goog-studio-main`,
  `sdklib/src/main/java/com/android/sdklib/tool/AvdManagerCli.java`
  (<https://android.googlesource.com/platform/tools/base/+/refs/heads/mirror-goog-studio-main/sdklib/src/main/java/com/android/sdklib/tool/AvdManagerCli.java>)
- SDK repo packaging (addon image zip layout `images/<abi>/…`):
  `build/make` @ `main`, `core/tasks/sdk-addon.mk`
  (<https://android.googlesource.com/platform/build/+/refs/heads/main/core/tasks/sdk-addon.mk>)
- sys-img repo XML generation and XSD selection:
  `development/build/tools/sdk_repo.mk` and `mk_sdk_repo_xml.sh`
  (<https://android.googlesource.com/platform/development/+/refs/heads/main/build/tools/sdk_repo.mk>)
- sys-img schema (legacy dialect) `prebuilts/devtools/repository/sdk-sys-img-03.xsd`
  (<https://android.googlesource.com/platform/prebuilts/devtools/+/refs/heads/main/repository/sdk-sys-img-03.xsd>)

Archive-layout verification (non-destructive HTTP Range read of zip central directories):

- <https://dl.google.com/android/repository/sys-img/google_apis_playstore/x86_64-36_r07.zip>
- <https://dl.google.com/android/repository/sys-img/google_apis_playstore/arm64-v8a-36_r07.zip>
- <https://dl.google.com/android/repository/sys-img/google_apis_playstore/x86_64-28_r08.zip>
- <https://dl.google.com/android/repository/sys-img/google_apis_playstore/x86-24_r19.zip>
- <https://dl.google.com/android/repository/sys-img/google_apis/x86_64-35_r09.zip>
- <https://dl.google.com/android/repository/sys-img/aosp_atd/x86_64-35_r01.zip>
