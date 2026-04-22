# morphe-apk-scraper

Auto-scrapes exact APK versions needed by [Morphe patches](https://github.com/MorpheApp/morphe-patches) and uploads them to GitHub Releases so the [Morphed-apps](https://github.com/myst-25/Morphed-apps) build workflow can use them as a reliable APK source.

## How it works

1. `apps.json` — list of all apps with their exact version required by the patches
2. `scraper.py` — scrapes APKMirror for each app, downloads the exact APK, uploads to this repo's GitHub Releases under the tag `apks`
3. `scrape.yml` — runs daily at 2 AM UTC, triggers `Morphed-apps` build workflow after scraping

## Setup

### Required Secrets

Add these secrets to this repo (`Settings → Secrets → Actions`):

| Secret | Description |
|--------|-------------|
| `GITHUB_TOKEN` | Auto-provided by GitHub Actions |
| `MORPHE_PAT` | Personal Access Token with `workflow` scope — needed to trigger the build in `Morphed-apps` |

### How to get MORPHE_PAT

1. Go to GitHub → Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens
2. Create token with access to `myst-25/Morphed-apps`
3. Grant **Actions: Read & Write** permission
4. Copy the token and add it as `MORPHE_PAT` secret in this repo

## APK Release

All scraped APKs are uploaded to the [`apks` release](https://github.com/myst-25/morphe-apk-scraper/releases/tag/apks) of this repo.

The `config.toml` in `Morphed-apps` points `archive-dlurl` to these assets.
