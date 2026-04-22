#!/usr/bin/env python3
"""
Morphe APK Scraper
Scrapes APKMirror for exact APK versions needed by Morphe patches
and uploads them to GitHub Releases.
"""

import json
import os
import re
import sys
import time
import subprocess
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

APKMIRROR_BASE = "https://www.apkmirror.com"
DOWNLOAD_DIR = Path("apks")
APPS_FILE = Path("apps.json")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "myst-25/morphe-apk-scraper")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
RELEASE_TAG = "apks"


def load_apps():
    with open(APPS_FILE) as f:
        return json.load(f)


def get_soup(url, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"  [!] Attempt {attempt+1} failed for {url}: {e}")
            time.sleep(5 * (attempt + 1))
    return None


def find_latest_version_url(base_url):
    """When no version is pinned, find the latest APK page URL from listing."""
    soup = get_soup(base_url)
    if not soup:
        return None
    link = soup.select_one(".appRowVariantTag~ .appRowVariantTag+ .table-cell a")
    if not link:
        # fallback: grab first release link
        link = soup.select_one('a[href*="-release/"]')
    if link:
        return APKMIRROR_BASE + link["href"]
    return None


def find_apk_download_page(release_url, arch):
    """From a release page, find the direct APK variant download page link."""
    soup = get_soup(release_url)
    if not soup:
        return None

    # Look for APK (not APKM/XAPK) download links
    for row in soup.select(".table-cell.rowheight"):
        text = row.get_text()
        # Prefer matching arch or nodpi / universal
        if arch and arch != "nodpi":
            if arch not in text and "universal" not in text.lower():
                continue
        # Skip bundles
        if "BUNDLE" in text.upper() or "APKS" in text.upper():
            continue
        link = row.find("a", href=re.compile(r"/apk/.+download/"))
        if link:
            return APKMIRROR_BASE + link["href"]

    # Fallback: find any APK download variant link on page
    link = soup.find("a", href=re.compile(r"/apk/.+download/"), string=re.compile(r"APK", re.I))
    if link:
        return APKMIRROR_BASE + link["href"]
    return None


def get_final_download_url(download_page_url):
    """From APKMirror download page, extract the final direct download URL."""
    soup = get_soup(download_page_url)
    if not soup:
        return None
    # The actual download button
    btn = soup.select_one("a[href*='?key=']") or soup.select_one(".downloadButton a") or \
          soup.find("a", href=re.compile(r"download\.php\?key="))
    if btn:
        href = btn.get("href", "")
        if href.startswith("/"):
            return APKMIRROR_BASE + href
        return href
    return None


def download_apk(url, dest_path, retries=3):
    """Download APK from APKMirror with retry."""
    for attempt in range(retries):
        try:
            with requests.get(url, headers=HEADERS, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            size = dest_path.stat().st_size
            if size < 100_000:  # suspiciously small = likely error page
                print(f"  [!] Downloaded file too small ({size} bytes), may be error page")
                dest_path.unlink(missing_ok=True)
                return False
            print(f"  [+] Downloaded {dest_path.name} ({size // 1024 // 1024} MB)")
            return True
        except Exception as e:
            print(f"  [!] Download attempt {attempt+1} failed: {e}")
            time.sleep(5 * (attempt + 1))
    return False


def get_or_create_release():
    """Get existing 'apks' release or create it. Returns release id."""
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    r = requests.get(api, headers=headers)
    if r.status_code == 200:
        return r.json()["id"], r.json()["upload_url"]
    # Create it
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    payload = {
        "tag_name": RELEASE_TAG,
        "name": "APK Mirror",
        "body": "Auto-scraped APKs for Morphe patching",
        "prerelease": False
    }
    r = requests.post(api, headers=headers, json=payload)
    r.raise_for_status()
    return r.json()["id"], r.json()["upload_url"]


def list_release_assets(release_id):
    """Returns dict of {filename: asset_id} already in the release."""
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    r = requests.get(api, headers=headers)
    r.raise_for_status()
    return {a["name"]: a["id"] for a in r.json()}


def delete_asset(asset_id):
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/assets/{asset_id}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    requests.delete(api, headers=headers)


def upload_asset(upload_url, file_path):
    """Upload APK file to GitHub release."""
    upload_url = upload_url.split("{")[0]  # strip template part
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/vnd.android.package-archive",
    }
    params = {"name": file_path.name}
    with open(file_path, "rb") as f:
        r = requests.post(upload_url, headers=headers, params=params, data=f, timeout=300)
    if r.status_code in (200, 201):
        print(f"  [+] Uploaded {file_path.name} to GitHub release")
        return r.json().get("browser_download_url", "")
    else:
        print(f"  [!] Upload failed ({r.status_code}): {r.text[:200]}")
        return ""


def scrape_and_upload(app):
    name = app["name"]
    package = app["package"]
    version = app.get("version")
    base_url = app["apkmirror_url"]
    arch = app.get("arch", "nodpi")

    print(f"\n[>] {name} ({package}) version={version or 'latest'}")

    # Determine release page URL
    if version:
        release_url = base_url
    else:
        print(f"  [~] No version pinned, finding latest...")
        release_url = find_latest_version_url(base_url)
        if not release_url:
            print(f"  [!] Could not find latest version URL for {name}")
            return None
        print(f"  [~] Latest release page: {release_url}")

    # Find APK variant download page
    dl_page = find_apk_download_page(release_url, arch)
    if not dl_page:
        print(f"  [!] Could not find APK download page for {name}")
        return None
    print(f"  [~] Download page: {dl_page}")

    # Get final download URL
    final_url = get_final_download_url(dl_page)
    if not final_url:
        print(f"  [!] Could not extract final download URL for {name}")
        return None
    print(f"  [~] Final URL: {final_url}")

    # Download APK
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    ver_tag = version.replace(" ", "_") if version else "latest"
    filename = f"{package}-{ver_tag}.apk"
    dest = DOWNLOAD_DIR / filename
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    if not download_apk(final_url, dest):
        print(f"  [!] Failed to download APK for {name}")
        return None

    return dest


def main():
    if not GITHUB_TOKEN:
        print("[!] GITHUB_TOKEN not set, cannot upload to releases")
        sys.exit(1)

    apps = load_apps()
    print(f"[*] Loaded {len(apps)} apps from apps.json")

    release_id, upload_url = get_or_create_release()
    print(f"[*] Using release id={release_id}")
    existing_assets = list_release_assets(release_id)
    print(f"[*] Existing assets: {list(existing_assets.keys())}")

    results = []
    for app in apps:
        apk_path = scrape_and_upload(app)
        if not apk_path:
            results.append({"name": app["name"], "status": "FAILED", "url": ""})
            continue

        # Delete old asset with same name if exists
        if apk_path.name in existing_assets:
            print(f"  [~] Replacing existing asset {apk_path.name}")
            delete_asset(existing_assets[apk_path.name])

        dl_url = upload_asset(upload_url, apk_path)
        results.append({
            "name": app["name"],
            "package": app["package"],
            "version": app.get("version", "latest"),
            "status": "OK" if dl_url else "UPLOAD_FAILED",
            "url": dl_url
        })

        # Clean up local file after upload
        apk_path.unlink(missing_ok=True)
        time.sleep(2)  # be polite to APKMirror

    # Write results summary
    with open("scrape_results.json", "w") as f:
        json.dump(results, f, indent=2)

    print("\n=== Scrape Summary ===")
    ok = sum(1 for r in results if r["status"] == "OK")
    failed = [r["name"] for r in results if r["status"] != "OK"]
    print(f"Success: {ok}/{len(results)}")
    if failed:
        print(f"Failed: {', '.join(failed)}")
        sys.exit(1)
    print("All APKs scraped and uploaded successfully!")


if __name__ == "__main__":
    main()
