# Publishing AVD Fleet to GitHub

This checkout has **not** been uploaded. There is no git repository, no GitHub
remote and no credentials in this workspace. The source tree, tests, docs and
release artifacts are ready; publishing needs a few things from the owner.

## What is needed

1. **A GitHub account and an empty repository** (public or private), e.g.
   `https://github.com/<owner>/avdfleet`. Do not initialise it with a README,
   `.gitignore` or licence if you plan to push this tree as-is.
2. **Git on this machine.** `git` is currently not installed. Install Git for
   Windows (or run the commands from any machine that has this tree).
3. **Authentication.** Use one of:
   - GitHub CLI: install `gh`, run `gh auth login`, then `gh repo create` or push.
   - HTTPS + a fine-grained personal access token with **Contents: read/write**
     for that repository (never paste the token into a chat or commit it).
   - SSH: add an SSH key to GitHub and use an `git@github.com:` remote.
4. **Optional:** GitHub Actions enabled for the repo (the workflows are already
   in `.github/workflows/`), and repository secrets only if you later add release
   signing/publishing.

## Commands

From `C:\Users\lab\Documents\Android`, after installing Git:

```powershell
git init -b main
git add .
git commit -m "AVDFleet v0.1.0"
git remote add origin https://github.com/<owner>/avdfleet.git
git push -u origin main
```

For private repositories use the same URL; Git will prompt for credentials.
With GitHub CLI:

```powershell
gh auth login
gh repo create <owner>/avdfleet --private --source . --remote origin --push
```

## What to upload where

- **Source repository:** all tracked source, tests, contracts, docs, research,
  `bootstrap/`, `ui/`, `.github/`, `LICENSE`, `README.md`, `BRIEF.md`,
  `BRIEF_ORIGINAL.md`, `DECISIONS.md`, `PLAN.md`, `FINAL_REPORT.md`.
- **Do not commit:** tokens, databases, vaults, SDKs, system images, emulator
  state, `dist/`, `node_modules/`, caches. The root-anchored `.gitignore`
  already covers these.
- **Release assets (optional):** upload the wheel, sdist and portable bundles
  from `dist/` to a GitHub Release instead of the repository. Include
  `SHA256SUMS.txt`.

## Before publishing

1. Run the local gates: `ruff check src tests bootstrap`, `mypy`,
   `pytest tests/unit tests/api tests/contract tests/integration tests/portable
   tests/cli tests/fakes bootstrap/test_bootstrap.py`, and in `ui/`:
   `pnpm typecheck && pnpm lint && pnpm test -- --run && pnpm build`.
2. Run the CI workflows on Linux/macOS/Windows; only Windows was exercised
   locally, so expect first-run fixes. The KVM emulator job is non-gating.
3. Review `FINAL_REPORT.md` §5 for known deviations: the GUI uses a hand-written
   design system instead of Mantine, and the TypeScript client is hand-written
   rather than generated from OpenAPI. Decide whether to keep those deviations
   for the first public release.
4. Confirm the Android SDK and Google system images are **not** committed or
   redistributed; the portable bundles deliberately contain only the app runtime
   and no Google components.
5. Choose repository visibility and licence (MIT is included).

## Pre-built source archive

`dist/avdfleet-0.1.0-source.zip` is a clean tree for upload/extraction; it
excludes runtime state, SDKs, caches, `dist/` and `node_modules/`.
