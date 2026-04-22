#!/usr/bin/env python3
"""
Morphe APK Scraper
Tries multiple sources in order: APKMirror -> Uptodown -> APKPure -> APKCombo
Uploads successfully downloaded APKs to GitHub Releases.
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
    "User-Agent": "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

APKMIRROR_BASE = "https://www.apkmirror.com"
UPTODOWN_BASE = "https://uptodown.com"
APKPURE_BASE = "https://apkpure.net"
APKCOMBO_BASE = "https://apkcombo.com"

DOWNLOAD_DIR = Path("apks")
APPS_FILE = Path("apps.json")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "myst-25/morphe-apk-scraper")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
RELEASE_TAG = "apks"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_apps():
    with open(APPS_FILE) as f:
        return json.load(f)


def get_soup(url, retries=3, delay=5, extra_headers=None):
    h = {**HEADERS, **(extra_headers or {})}
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=h, timeout=30, allow_redirects=True)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 30))
                print(f"    [rate-limit] waiting {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"    [!] attempt {attempt+1} failed: {e}")
            time.sleep(delay * (attempt + 1))
    return None


def ver_slug(version):
    """'20.47.62' -> '20-47-62'"""
    return re.sub(r"[^a-zA-Z0-9]+", "-", version).strip("-").lower()


def download_file(url, dest_path, retries=3, extra_headers=None):
    h = {**HEADERS, **(extra_headers or {})}
    for attempt in range(retries):
        try:
            with requests.get(url, headers=h, stream=True,
                              timeout=180, allow_redirects=True) as r:
                r.raise_for_status()
                ct = r.headers.get("Content-Type", "")
                if "text/html" in ct:
                    print(f"    [!] Got HTML page instead of APK — likely blocked")
                    return False
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
            size = dest_path.stat().st_size
            if size < 500_000:
                print(f"    [!] File too small ({size} bytes), not a valid APK")
                dest_path.unlink(missing_ok=True)
                return False
            print(f"    [+] Downloaded {dest_path.name} ({size // 1024 // 1024} MB)")
            return True
        except Exception as e:
            print(f"    [!] Download attempt {attempt+1} failed: {e}")
            time.sleep(8 * (attempt + 1))
    return False


# ---------------------------------------------------------------------------
# Source 1: APKMirror
# ---------------------------------------------------------------------------

def apkmirror_get(app):
    base = app["apkmirror_url"].rstrip("/") + "/"
    version = app.get("version")
    arch = app.get("arch", "nodpi")
    package = app["package"]

    print("  [APKMirror] trying...")

    # Step 1: Find release page
    soup = get_soup(base)
    if not soup:
        return None

    release_page = None
    if version:
        slug = ver_slug(version)
        for a in soup.find_all("a", href=True):
            if slug in a["href"] and "/apk/" in a["href"] and "download" not in a["href"]:
                release_page = APKMIRROR_BASE + a["href"] if a["href"].startswith("/") else a["href"]
                break
        if not release_page:
            # construct directly
            app_slug = base.rstrip("/").split("/")[-1]
            release_page = f"{base}{app_slug}-{slug}-release/"
    else:
        for a in soup.find_all("a", href=re.compile(r"-release/$")):
            release_page = APKMIRROR_BASE + a["href"] if a["href"].startswith("/") else a["href"]
            break

    if not release_page:
        print("    [!] Could not find release page")
        return None
    print(f"    release_page={release_page}")

    # Step 2: Find variant page (individual APK)
    soup2 = get_soup(release_page)
    if not soup2:
        return None

    variant_page = None
    candidates = []
    for a in soup2.find_all("a", href=re.compile(r"/apk/.+/\d+/$")):
        parent_text = (a.find_parent() or a).get_text(" ", strip=True).upper()
        if "BUNDLE" in parent_text or "APKM" in parent_text:
            continue
        candidates.append((a["href"], parent_text))

    def score(item):
        href, text = item
        t = text.lower()
        if arch and arch != "nodpi" and arch.lower() in t:
            return 0
        if "nodpi" in t or "universal" in t:
            return 1
        return 2

    if candidates:
        candidates.sort(key=score)
        href = candidates[0][0]
        variant_page = APKMIRROR_BASE + href if href.startswith("/") else href
    if not variant_page:
        print("    [!] No variant page found")
        return None
    print(f"    variant_page={variant_page}")

    # Step 3: Interstitial download page
    soup3 = get_soup(variant_page)
    if not soup3:
        return None
    btn = soup3.find("a", href=re.compile(r"download/\?key="))
    if not btn:
        print("    [!] No download button found")
        return None
    interstitial = APKMIRROR_BASE + btn["href"] if btn["href"].startswith("/") else btn["href"]
    print(f"    interstitial={interstitial}")

    # Step 4: Final CDN URL
    soup4 = get_soup(interstitial)
    if not soup4:
        return None
    final = None
    for a in soup4.find_all("a", href=True):
        if "cdn.apkmirror.com" in a["href"] or re.search(r"\.apk(\?|$)", a["href"]):
            final = a["href"]
            break
    if not final:
        print("    [!] Final URL not found")
        return None
    print(f"    final_url={final}")

    dest = DOWNLOAD_DIR / f"{package}-{(version or 'latest').replace(' ', '_')}.apk"
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    return dest if download_file(final, dest) else None


# ---------------------------------------------------------------------------
# Source 2: Uptodown
# ---------------------------------------------------------------------------

def uptodown_get(app):
    uptodown_url = app.get("uptodown_dlurl")
    version = app.get("version")
    package = app["package"]

    if not uptodown_url:
        return None

    print("  [Uptodown] trying...")
    base = uptodown_url.rstrip("/")

    # Uptodown version page pattern: {base}/versions
    versions_url = f"{base}/versions"
    soup = get_soup(versions_url)
    if not soup:
        # try direct download page
        soup = get_soup(base)
        if not soup:
            return None

    # Find the version download link
    dl_url = None
    if version:
        # Look for link containing exact version text
        for a in soup.find_all("a", href=True):
            if version in a.get_text() or version in a["href"]:
                dl_url = a["href"]
                break
    if not dl_url:
        # Latest: find first .apk or /download/ link
        for a in soup.find_all("a", href=re.compile(r"/(download|post-download)/")):
            dl_url = a["href"]
            break

    if not dl_url:
        print("    [!] No download link found on Uptodown")
        return None

    if not dl_url.startswith("http"):
        from urllib.parse import urljoin
        dl_url = urljoin(base, dl_url)
    print(f"    dl_url={dl_url}")

    # Navigate to download page to get direct link
    soup2 = get_soup(dl_url)
    final = None
    if soup2:
        btn = soup2.find("a", id="detail-download-button") or \
              soup2.find("a", href=re.compile(r"\.apk"))
        if btn:
            final = btn["href"]
            if not final.startswith("http"):
                from urllib.parse import urljoin
                final = urljoin(dl_url, final)
    if not final:
        final = dl_url  # try directly

    print(f"    final_url={final}")
    dest = DOWNLOAD_DIR / f"{package}-{(version or 'latest').replace(' ', '_')}.apk"
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    return dest if download_file(final, dest) else None


# ---------------------------------------------------------------------------
# Source 3: APKPure
# ---------------------------------------------------------------------------

def apkpure_get(app):
    package = app["package"]
    version = app.get("version")
    print("  [APKPure] trying...")

    # APKPure search URL
    search_url = f"https://apkpure.net/search?q={package}"
    soup = get_soup(search_url)
    if not soup:
        return None

    # Find app page link
    app_link = None
    for a in soup.find_all("a", href=re.compile(r"/" + re.escape(package.split(".")[-1].lower()))):
        app_link = a["href"]
        break
    if not app_link:
        # Direct URL guess
        app_name_slug = package.replace(".", "-").lower()
        app_link = f"https://apkpure.net/{app_name_slug}/{package}"
    elif not app_link.startswith("http"):
        app_link = "https://apkpure.net" + app_link

    print(f"    app_page={app_link}")

    # Get download page
    dl_page = f"{app_link}/download"
    if version:
        dl_page = f"{app_link}/{version}/download"

    soup2 = get_soup(dl_page)
    if not soup2:
        soup2 = get_soup(app_link)
        if not soup2:
            return None

    # Find APK download link
    final = None
    for a in soup2.find_all("a", href=True):
        href = a["href"]
        if ".apk" in href and ("download" in href or "dw.apkpure" in href):
            final = href
            break
    if not final:
        btn = soup2.find("a", id="download_link") or soup2.find("a", class_=re.compile(r"download"))
        if btn:
            final = btn.get("href", "")

    if not final:
        print("    [!] No download link found on APKPure")
        return None

    if not final.startswith("http"):
        final = "https://apkpure.net" + final
    print(f"    final_url={final}")

    dest = DOWNLOAD_DIR / f"{package}-{(version or 'latest').replace(' ', '_')}.apk"
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    return dest if download_file(final, dest) else None


# ---------------------------------------------------------------------------
# Source 4: APKCombo
# ---------------------------------------------------------------------------

def apkcombo_get(app):
    package = app["package"]
    version = app.get("version")
    print("  [APKCombo] trying...")

    app_url = f"https://apkcombo.com/apk/{package}"
    if version:
        app_url = f"https://apkcombo.com/apk/{package}/{version}"

    soup = get_soup(app_url)
    if not soup:
        return None

    # Find direct APK download link
    final = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".apk" in href and ("download" in href or "apkcombo" in href or "filedownload" in href):
            final = href
            break
    if not final:
        a = soup.find("a", class_=re.compile(r"download", re.I))
        if a:
            final = a.get("href", "")

    if not final:
        print("    [!] No download link found on APKCombo")
        return None

    if not final.startswith("http"):
        final = "https://apkcombo.com" + final
    print(f"    final_url={final}")

    dest = DOWNLOAD_DIR / f"{package}-{(version or 'latest').replace(' ', '_')}.apk"
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    return dest if download_file(final, dest) else None


# ---------------------------------------------------------------------------
# GitHub Release helpers
# ---------------------------------------------------------------------------

def get_or_create_release():
    gh = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    base = f"https://api.github.com/repos/{GITHUB_REPO}"
    r = requests.get(f"{base}/releases/tags/{RELEASE_TAG}", headers=gh)
    if r.status_code == 200:
        d = r.json()
        return d["id"], d["upload_url"]
    r = requests.post(f"{base}/releases", headers=gh, json={
        "tag_name": RELEASE_TAG,
        "name": "APK Mirror",
        "body": "Auto-scraped APKs for Morphe patching. Do not edit manually.",
        "prerelease": False
    })
    r.raise_for_status()
    d = r.json()
    return d["id"], d["upload_url"]


def list_assets(release_id):
    gh = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets",
        headers=gh
    )
    r.raise_for_status()
    return {a["name"]: a["id"] for a in r.json()}


def delete_asset(asset_id):
    gh = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    requests.delete(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/assets/{asset_id}",
        headers=gh
    )


def upload_asset(upload_url, file_path):
    url = re.sub(r"\{.*?\}", "", upload_url)
    gh = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/vnd.android.package-archive",
        "Accept": "application/vnd.github+json"
    }
    with open(file_path, "rb") as f:
        r = requests.post(url, headers=gh,
                          params={"name": file_path.name},
                          data=f, timeout=600)
    if r.status_code in (200, 201):
        dl = r.json().get("browser_download_url", "")
        print(f"    [+] Uploaded: {dl}")
        return dl
    print(f"    [!] Upload failed ({r.status_code}): {r.text[:300]}")
    return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def try_all_sources(app):
    """Try APKMirror -> Uptodown -> APKPure -> APKCombo in order."""
    sources = [
        ("APKMirror", apkmirror_get),
        ("Uptodown",  uptodown_get),
        ("APKPure",   apkpure_get),
        ("APKCombo",  apkcombo_get),
    ]
    for source_name, fn in sources:
        try:
            result = fn(app)
            if result and result.exists():
                print(f"  [OK] Got APK from {source_name}")
                return result, source_name
        except Exception as e:
            print(f"  [!] {source_name} threw exception: {e}")
        time.sleep(2)
    return None, None


def main():
    if not GITHUB_TOKEN:
        print("[!] GITHUB_TOKEN not set")
        sys.exit(1)

    apps = load_apps()
    print(f"[*] {len(apps)} apps to process")

    release_id, upload_url = get_or_create_release()
    print(f"[*] Release id={release_id}")
    existing = list_assets(release_id)
    print(f"[*] Existing assets in release: {len(existing)}")

    results = []
    for app in apps:
        name = app["name"]
        package = app["package"]
        version = app.get("version", "latest")
        print(f"\n[>>>] {name} | {package} | v{version}")

        apk_path, source = try_all_sources(app)

        if not apk_path:
            print(f"  [FAIL] All sources failed for {name}")
            results.append({"name": name, "status": "FAILED", "source": None, "url": ""})
            time.sleep(2)
            continue

        # Replace old asset
        if apk_path.name in existing:
            print(f"  [~] Replacing existing asset {apk_path.name}")
            delete_asset(existing[apk_path.name])

        dl_url = upload_asset(upload_url, apk_path)
        results.append({
            "name": name,
            "package": package,
            "version": version,
            "source": source,
            "status": "OK" if dl_url else "UPLOAD_FAILED",
            "url": dl_url
        })
        apk_path.unlink(missing_ok=True)
        time.sleep(3)

    with open("scrape_results.json", "w") as f:
        json.dump(results, f, indent=2)

    print("\n=== Summary ===")
    ok = [r for r in results if r["status"] == "OK"]
    failed = [r["name"] for r in results if r["status"] != "OK"]
    print(f"OK: {len(ok)}/{len(results)}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        sys.exit(1)
    print("All done!")


if __name__ == "__main__":
    main()
