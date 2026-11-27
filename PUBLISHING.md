# Publishing AVD Fleet to GitHub

The source tree is published at
<https://github.com/willokocha-spec/AVD-Fleet-Manager> (public, `main`). This file
records the prerequisites and how to publish updates/release assets. No credentials
or private keys live in the repository.

## What was used

- GitHub repository: `willokocha-spec/AVD-Fleet-Manager` (public, empty at push time)
- SSH deploy key converted from the operator-supplied PPK; the converted key and
  PPK are outside the repository and must be rotated by the operator
- Git 2.56.1 portable (MinGit) for the push

## What is needed for future updates

1. **Git** on the machine (portable MinGit is enough) or GitHub Desktop/CLI.
2. **Authentication**: the repository deploy key, an account SSH key, `gh auth
   login`, or a fine-grained PAT with Contents read/write. Never commit or paste a
   token/private key.
3. Optional: GitHub Actions enabled for CI and release workflows.
4. Optional: a PAT or `gh` login to upload the wheel/sdist/portable ZIPs as
   **GitHub Release assets**. SSH alone cannot create releases or upload assets.

## Update commands

From `C:\Users\lab\Documents\Android`, with Git available and the SSH key loaded:

```powershell
git add .
git commit -m "Update AVD Fleet"
git push origin main
```

The remote is already configured:

```text
git@github.com:willokocha-spec/AVD-Fleet-Manager.git
```

## Release assets

`dist/` contains the wheel, sdist, source archive and the Windows x64/ARM64
portable ZIPs. They are intentionally **not committed** (`.gitignore` root `dist/`).
Publish them through the GitHub Releases UI or `gh release upload`, with
`dist/SHA256SUMS.txt`.

## Before publishing any update

1. Run the local gates: `ruff check src tests bootstrap`, `mypy`,
   `pytest tests/unit tests/api tests/contract tests/integration tests/portable
   tests/cli tests/fakes bootstrap/test_bootstrap.py`, and in `ui/`:
   `pnpm typecheck && pnpm lint && pnpm test -- --run && pnpm build`.
2. Run CI on Linux/macOS/Windows; only Windows was exercised locally.
3. Review `FINAL_REPORT.md` §5 for known deviations (hand-written UI instead of
   Mantine, hand-written client instead of generated OpenAPI client).
4. Confirm Android SDKs, system images, tokens, private keys and runtime state are
   not committed. `git status --short` should be clean before tagging.
