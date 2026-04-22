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
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://www.apkmirror.com/",
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


def get_soup(url, retries=3, delay=4):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 30))
                print(f"  [!] Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"  [!] Attempt {attempt+1} failed for {url}: {e}")
            time.sleep(delay * (attempt + 1))
    return None


def version_to_url_slug(version):
    """Convert version string like '20.47.62' -> '20-47-62'"""
    return re.sub(r"[^a-zA-Z0-9]+", "-", version).strip("-").lower()


def find_release_page(base_url, version):
    """
    Given the APKMirror listing URL and a version string,
    find the correct release page URL.
    Strategy:
      1. Load listing page and search for a link containing the version slug.
      2. Fallback: construct URL from base_url + version slug pattern.
    """
    soup = get_soup(base_url)
    if not soup:
        return None

    ver_slug = version_to_url_slug(version)
    # Search all links on the page for one matching the version
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ver_slug in href and "/apk/" in href and href.endswith("/"):
            # Make sure it's a release page (not download page)
            if "download" not in href:
                full = href if href.startswith("http") else APKMIRROR_BASE + href
                print(f"  [~] Found release page via listing: {full}")
                return full

    # Fallback: try to construct the URL directly
    # APKMirror pattern: {base_url}{app-slug}-{version-slug}-release/
    # Extract app slug from base_url
    base_clean = base_url.rstrip("/")
    app_slug = base_clean.split("/")[-1]
    constructed = f"{base_clean}/{app_slug}-{ver_slug}-release/"
    print(f"  [~] Trying constructed URL: {constructed}")
    resp = requests.get(constructed, headers=HEADERS, timeout=20)
    if resp.status_code == 200 and "apkmirror" in resp.url:
        return constructed

    print(f"  [!] Could not find release page for version {version}")
    return None


def find_latest_release_page(base_url):
    """When no version is pinned, get the first/latest release page URL."""
    soup = get_soup(base_url)
    if not soup:
        return None
    # APKMirror listing: release rows have class 'appRow'
    for a in soup.select(".appRow a[href]"):
        href = a["href"]
        if "-release/" in href and "download" not in href:
            full = href if href.startswith("http") else APKMIRROR_BASE + href
            return full
    # Fallback: any link with -release/
    a = soup.find("a", href=re.compile(r"-release/$"))
    if a:
        href = a["href"]
        return href if href.startswith("http") else APKMIRROR_BASE + href
    return None


def find_apk_variant_page(release_url, arch):
    """
    From a release page (e.g. /apk/google-inc/youtube/youtube-20-47-62-release/),
    find the individual APK variant download info page.
    APKMirror shows a table of variants; we pick APK (not APKM/bundle), matching arch.
    """
    soup = get_soup(release_url)
    if not soup:
        return None

    # The variants table rows — each has a link to the variant info page
    # Real APKMirror selector: div.table-cell > span contains arch info,
    # and the row has a link to /apk/.../download-variant/
    rows = soup.select("div.variants-table .table-row")
    if not rows:
        # fallback selector used on some pages
        rows = soup.select(".apkm-badge")

    # Strategy: collect all APK (non-bundle) variant links
    candidates = []
    for a in soup.find_all("a", href=re.compile(r"/apk/.+/\d+/$")):
        href = a["href"]
        # Get surrounding text to check arch and type
        parent_text = a.find_parent().get_text(" ", strip=True) if a.find_parent() else ""
        # Skip APKM bundles
        if "BUNDLE" in parent_text.upper() or "APKM" in parent_text.upper():
            continue
        candidates.append((href, parent_text))

    if not candidates:
        # Try broader: any link ending with digit/
        for a in soup.find_all("a", href=re.compile(r"/apk/")):
            href = a["href"]
            if re.search(r"/\d+/$", href):
                parent_text = a.find_parent().get_text(" ", strip=True) if a.find_parent() else ""
                if "BUNDLE" not in parent_text.upper():
                    candidates.append((href, parent_text))

    if not candidates:
        print(f"  [!] No variant links found on {release_url}")
        return None

    # Prefer arch match, then nodpi/universal, then first
    def score(item):
        href, text = item
        t = text.lower()
        if arch and arch != "nodpi" and arch.lower() in t:
            return 0
        if "nodpi" in t or "universal" in t or "all" in t:
            return 1
        return 2

    candidates.sort(key=score)
    best_href = candidates[0][0]
    full = best_href if best_href.startswith("http") else APKMIRROR_BASE + best_href
    print(f"  [~] Variant page: {full}")
    return full


def get_download_page_url(variant_page_url):
    """
    From a variant info page (/apk/.../{id}/),
    find the 'Download APK' button link which leads to the interstitial download page.
    """
    soup = get_soup(variant_page_url)
    if not soup:
        return None

    # APKMirror: the green download button links to a page like
    # /apk/.../download/?key=...
    btn = soup.find("a", href=re.compile(r"download/\?key="))
    if btn:
        href = btn["href"]
        full = href if href.startswith("http") else APKMIRROR_BASE + href
        print(f"  [~] Interstitial page: {full}")
        return full

    # Fallback: look for any download link
    btn = soup.find("a", string=re.compile(r"download", re.I), href=re.compile(r"download"))
    if btn:
        href = btn["href"]
        full = href if href.startswith("http") else APKMIRROR_BASE + href
        return full

    print(f"  [!] No download button found on {variant_page_url}")
    return None


def get_final_apk_url(interstitial_url):
    """
    APKMirror interstitial page has a 'Click here to download' link
    that is the actual APK file URL (via their CDN/redirect).
    The real link is in: a[rel='nofollow'] or href containing 'cdn.apkmirror.com'
    """
    soup = get_soup(interstitial_url)
    if not soup:
        return None

    # Direct CDN link
    a = soup.find("a", href=re.compile(r"cdn\.apkmirror\.com"))
    if a:
        return a["href"]

    # Fallback: nofollow download link
    a = soup.find("a", rel="nofollow", href=re.compile(r"\.apk"))
    if a:
        href = a["href"]
        return href if href.startswith("http") else APKMIRROR_BASE + href

    # Last resort: any .apk link
    a = soup.find("a", href=re.compile(r"\.apk"))
    if a:
        href = a["href"]
        return href if href.startswith("http") else APKMIRROR_BASE + href

    # Try extracting from onclick / data attrs
    for tag in soup.find_all(attrs={"data-google-interstitial": True}):
        href = tag.get("href", "")
        if href:
            return href if href.startswith("http") else APKMIRROR_BASE + href

    print(f"  [!] Could not find final APK URL on {interstitial_url}")
    return None


def download_apk(url, dest_path, retries=3):
    """Download APK with retry and size validation."""
    for attempt in range(retries):
        try:
            with requests.get(url, headers=HEADERS, stream=True,
                              timeout=180, allow_redirects=True) as r:
                r.raise_for_status()
                content_type = r.headers.get("Content-Type", "")
                if "text/html" in content_type:
                    print(f"  [!] Got HTML instead of APK (blocked/captcha?)")
                    return False
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
            size = dest_path.stat().st_size
            if size < 500_000:
                print(f"  [!] File too small ({size} bytes), likely not a valid APK")
                dest_path.unlink(missing_ok=True)
                return False
            print(f"  [+] Downloaded {dest_path.name} ({size // 1024 // 1024} MB)")
            return True
        except Exception as e:
            print(f"  [!] Download attempt {attempt+1} failed: {e}")
            time.sleep(8 * (attempt + 1))
    return False


def get_or_create_release():
    api_base = f"https://api.github.com/repos/{GITHUB_REPO}"
    gh_headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    r = requests.get(f"{api_base}/releases/tags/{RELEASE_TAG}", headers=gh_headers)
    if r.status_code == 200:
        data = r.json()
        return data["id"], data["upload_url"]
    # Create release + tag
    payload = {
        "tag_name": RELEASE_TAG,
        "name": "APK Mirror",
        "body": "Auto-scraped APKs for Morphe patching. Do not edit manually.",
        "prerelease": False
    }
    r = requests.post(f"{api_base}/releases", headers=gh_headers, json=payload)
    r.raise_for_status()
    data = r.json()
    return data["id"], data["upload_url"]


def list_release_assets(release_id):
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets"
    gh_headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    r = requests.get(api, headers=gh_headers)
    r.raise_for_status()
    return {a["name"]: a["id"] for a in r.json()}


def delete_asset(asset_id):
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/assets/{asset_id}"
    gh_headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    requests.delete(api, headers=gh_headers)


def upload_asset(upload_url, file_path):
    upload_url = re.sub(r"\{.*\}", "", upload_url)  # strip {?name,label} template
    gh_headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/vnd.android.package-archive",
        "Accept": "application/vnd.github+json"
    }
    params = {"name": file_path.name, "label": file_path.name}
    with open(file_path, "rb") as f:
        r = requests.post(upload_url, headers=gh_headers,
                          params=params, data=f, timeout=600)
    if r.status_code in (200, 201):
        url = r.json().get("browser_download_url", "")
        print(f"  [+] Uploaded to release: {url}")
        return url
    print(f"  [!] Upload failed ({r.status_code}): {r.text[:300]}")
    return ""


def scrape_app(app):
    name = app["name"]
    package = app["package"]
    version = app.get("version")
    base_url = app["apkmirror_url"].rstrip("/") + "/"
    arch = app.get("arch", "nodpi")

    print(f"\n[>] {name} | pkg={package} | ver={version or 'latest'} | arch={arch}")

    # Step 1: Find release page
    if version:
        release_page = find_release_page(base_url, version)
    else:
        release_page = find_latest_release_page(base_url)

    if not release_page:
        print(f"  [FAIL] Could not find release page")
        return None

    # Step 2: Find APK variant page
    variant_page = find_apk_variant_page(release_page, arch)
    if not variant_page:
        print(f"  [FAIL] Could not find variant page")
        return None

    # Step 3: Get interstitial download page
    interstitial = get_download_page_url(variant_page)
    if not interstitial:
        print(f"  [FAIL] Could not find download page")
        return None

    # Step 4: Get final APK CDN URL
    final_url = get_final_apk_url(interstitial)
    if not final_url:
        print(f"  [FAIL] Could not get final APK URL")
        return None
    print(f"  [~] Final APK URL: {final_url}")

    # Step 5: Download
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    ver_tag = (version or "latest").replace(" ", "_")
    filename = f"{package}-{ver_tag}.apk"
    dest = DOWNLOAD_DIR / filename

    if not download_apk(final_url, dest):
        print(f"  [FAIL] Download failed")
        return None

    return dest


def main():
    if not GITHUB_TOKEN:
        print("[!] GITHUB_TOKEN not set")
        sys.exit(1)

    apps = load_apps()
    print(f"[*] Loaded {len(apps)} apps")

    release_id, upload_url = get_or_create_release()
    print(f"[*] Release id={release_id}")
    existing = list_release_assets(release_id)
    print(f"[*] Existing assets: {len(existing)}")

    results = []
    for app in apps:
        apk_path = scrape_app(app)
        if not apk_path:
            results.append({"name": app["name"], "status": "FAILED", "url": ""})
            time.sleep(3)
            continue

        # Replace old asset if exists
        if apk_path.name in existing:
            print(f"  [~] Deleting old asset: {apk_path.name}")
            delete_asset(existing[apk_path.name])

        dl_url = upload_asset(upload_url, apk_path)
        results.append({
            "name": app["name"],
            "package": app["package"],
            "version": app.get("version", "latest"),
            "status": "OK" if dl_url else "UPLOAD_FAILED",
            "url": dl_url
        })

        apk_path.unlink(missing_ok=True)
        time.sleep(3)  # polite delay between apps

    with open("scrape_results.json", "w") as f:
        json.dump(results, f, indent=2)

    print("\n=== Summary ===")
    ok = [r for r in results if r["status"] == "OK"]
    failed = [r["name"] for r in results if r["status"] != "OK"]
    print(f"OK: {len(ok)}/{len(results)}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        sys.exit(1)
    print("Done!")


if __name__ == "__main__":
    main()
