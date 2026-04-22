#!/usr/bin/env python3
"""
Morphe APK Scraper
Sources (in order): APKMirror -> Uptodown -> APKCombo
Uploads APKs to GitHub Releases tag 'apks'.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

DOWNLOAD_DIR = Path("apks")
APPS_FILE    = Path("apps.json")
GITHUB_REPO  = os.environ.get("GITHUB_REPOSITORY", "myst-25/morphe-apk-scraper")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
RELEASE_TAG  = "apks"

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ── helpers ────────────────────────────────────────────────────────────────

def load_apps():
    with open(APPS_FILE) as f:
        return json.load(f)


def get(url, retries=3, delay=5):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=30, allow_redirects=True)
            print(f"    GET {url}  ->  HTTP {r.status_code}")
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 30))
                print(f"    rate-limited, sleeping {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        except Exception as e:
            print(f"    attempt {i+1} error: {e}")
            time.sleep(delay * (i + 1))
    return None


def soup(url):
    r = get(url)
    return BeautifulSoup(r.text, "html.parser") if r else None


def ver_slug(v):
    return re.sub(r"[^a-zA-Z0-9]+", "-", v).strip("-").lower()


def download(url, dest, retries=3):
    for i in range(retries):
        try:
            with SESSION.get(url, stream=True, timeout=180,
                             allow_redirects=True) as r:
                ct = r.headers.get("Content-Type", "")
                print(f"    DL {url[:80]}  ct={ct}  status={r.status_code}")
                if "text/html" in ct or r.status_code >= 400:
                    print(f"    blocked or error, skipping")
                    return False
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
            size = dest.stat().st_size
            if size < 500_000:
                print(f"    too small ({size} B), not valid APK")
                dest.unlink(missing_ok=True)
                return False
            print(f"    saved {dest.name}  ({size//1024//1024} MB)")
            return True
        except Exception as e:
            print(f"    download attempt {i+1} error: {e}")
            time.sleep(8 * (i + 1))
    return False


# ── Source 1: APKMirror ────────────────────────────────────────────────────

def apkmirror(app):
    print("  [APKMirror]")
    base    = app["apkmirror_url"].rstrip("/") + "/"
    version = app.get("version")
    arch    = app.get("arch", "nodpi")
    package = app["package"]

    # 1. release page
    release_page = None
    if version:
        s = soup(base)
        if not s:
            return None
        slug = ver_slug(version)
        for a in s.find_all("a", href=True):
            h = a["href"]
            if slug in h and "/apk/" in h and "download" not in h:
                release_page = ("https://www.apkmirror.com" + h
                                if h.startswith("/") else h)
                break
        if not release_page:
            app_slug = base.rstrip("/").split("/")[-1]
            release_page = f"{base}{app_slug}-{slug}-release/"
    else:
        s = soup(base)
        if not s:
            return None
        a = s.find("a", href=re.compile(r"-release/$"))
        if a:
            h = a["href"]
            release_page = ("https://www.apkmirror.com" + h
                            if h.startswith("/") else h)
    if not release_page:
        print("    no release page found")
        return None
    print(f"    release_page={release_page}")

    # 2. variant page
    s2 = soup(release_page)
    if not s2:
        return None
    candidates = []
    for a in s2.find_all("a", href=re.compile(r"/apk/.+/\d+/$")):
        pt = (a.find_parent() or a).get_text(" ", strip=True).upper()
        if "BUNDLE" in pt or "APKM" in pt:
            continue
        candidates.append((a["href"], pt))

    def score(item):
        h, t = item
        t = t.lower()
        if arch and arch != "nodpi" and arch.lower() in t:
            return 0
        if "nodpi" in t or "universal" in t:
            return 1
        return 2

    if not candidates:
        print("    no variant candidates")
        return None
    candidates.sort(key=score)
    vh = candidates[0][0]
    variant_page = ("https://www.apkmirror.com" + vh
                    if vh.startswith("/") else vh)
    print(f"    variant_page={variant_page}")

    # 3. interstitial
    s3 = soup(variant_page)
    if not s3:
        return None
    btn = s3.find("a", href=re.compile(r"download/\?key="))
    if not btn:
        print("    no download button")
        return None
    ih = btn["href"]
    interstitial = ("https://www.apkmirror.com" + ih
                    if ih.startswith("/") else ih)
    print(f"    interstitial={interstitial}")

    # 4. CDN url
    s4 = soup(interstitial)
    if not s4:
        return None
    final = None
    for a in s4.find_all("a", href=True):
        if "cdn.apkmirror.com" in a["href"] or re.search(r"\.apk(\?|$)", a["href"]):
            final = a["href"]
            break
    if not final:
        print("    no final CDN url")
        return None
    print(f"    final={final}")

    dest = DOWNLOAD_DIR / f"{package}-{(version or 'latest').replace(' ','_')}.apk"
    return dest if download(final, dest) else None


# ── Source 2: Uptodown ─────────────────────────────────────────────────────

def uptodown(app):
    print("  [Uptodown]")
    base    = app.get("uptodown_dlurl", "").rstrip("/")
    version = app.get("version")
    package = app["package"]
    if not base:
        print("    no uptodown_dlurl")
        return None

    # Uptodown versions page
    versions_url = f"{base}/versions"
    s = soup(versions_url)
    if not s:
        s = soup(base)
    if not s:
        return None

    # Find download page for the specific version
    dl_page = None
    if version:
        # version rows look like: /android/post-download/XXXXX
        for a in s.find_all("a", href=True):
            text = a.get_text(strip=True)
            if version in text:
                dl_page = a["href"]
                break
        if not dl_page:
            # try direct download URL pattern
            # Uptodown stores version in the download slug
            for a in s.find_all("a", href=re.compile(r"post-download|/download")):
                parent_text = (a.find_parent() or a).get_text(" ", strip=True)
                if version in parent_text:
                    dl_page = a["href"]
                    break
    if not dl_page:
        # fallback: grab the first download link (latest)
        a = s.find("a", href=re.compile(r"post-download|/download"))
        if a:
            dl_page = a["href"]

    if not dl_page:
        print("    no download page link found")
        return None

    if not dl_page.startswith("http"):
        dl_page = urljoin(base, dl_page)
    print(f"    dl_page={dl_page}")

    # Hit the download page to get the real APK link
    s2 = soup(dl_page)
    final = None
    if s2:
        # Uptodown puts the APK link in a button or meta refresh
        btn = (s2.find("a", id="detail-download-button") or
               s2.find("a", attrs={"data-url": True}) or
               s2.find("a", href=re.compile(r"\.apk")))
        if btn:
            final = btn.get("href") or btn.get("data-url", "")
        # meta refresh fallback
        if not final:
            meta = s2.find("meta", attrs={"http-equiv": "refresh"})
            if meta:
                content = meta.get("content", "")
                m = re.search(r"url=(.+)", content, re.I)
                if m:
                    final = m.group(1).strip()

    if not final:
        # last resort: try direct download from Uptodown CDN pattern
        # https://{app}.en.uptodown.com/android/download/{id}
        print("    no final link from download page, trying direct")
        final = dl_page

    if not final.startswith("http"):
        final = urljoin(base, final)
    print(f"    final={final}")

    dest = DOWNLOAD_DIR / f"{package}-{(version or 'latest').replace(' ','_')}.apk"
    return dest if download(final, dest) else None


# ── Source 3: APKCombo ─────────────────────────────────────────────────────

def apkcombo(app):
    print("  [APKCombo]")
    package = app["package"]
    version = app.get("version")
    base_url = app.get("apkcombo_url", f"https://apkcombo.com/apk/{package}")

    url = base_url
    if version:
        url = f"{base_url}/{version}"
    print(f"    url={url}")

    s = soup(url)
    if not s:
        s = soup(base_url)
    if not s:
        return None

    # APKCombo: look for direct .apk href or a download button
    final = None
    for a in s.find_all("a", href=True):
        h = a["href"]
        if re.search(r"\.apk(\?|$)", h):
            final = h
            break
    if not final:
        # APKCombo download button is often in a form or data attr
        for tag in s.find_all(attrs={"data-src": re.compile(r"\.apk")}):
            final = tag["data-src"]
            break
    if not final:
        btn = s.find("a", class_=re.compile(r"download", re.I))
        if btn:
            final = btn.get("href", "")

    if not final:
        print("    no download link found")
        return None

    if not final.startswith("http"):
        final = "https://apkcombo.com" + final
    print(f"    final={final}")

    dest = DOWNLOAD_DIR / f"{package}-{(version or 'latest').replace(' ','_')}.apk"
    return dest if download(final, dest) else None


# ── GitHub Release helpers ─────────────────────────────────────────────────

def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

def get_or_create_release():
    base = f"https://api.github.com/repos/{GITHUB_REPO}"
    r = requests.get(f"{base}/releases/tags/{RELEASE_TAG}", headers=gh_headers())
    if r.status_code == 200:
        d = r.json()
        return d["id"], d["upload_url"]
    r = requests.post(f"{base}/releases", headers=gh_headers(), json={
        "tag_name": RELEASE_TAG,
        "name": "APK Mirror",
        "body": "Auto-scraped APKs for Morphe patching.",
        "prerelease": False
    })
    r.raise_for_status()
    d = r.json()
    return d["id"], d["upload_url"]

def list_assets(release_id):
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets",
        headers=gh_headers()
    )
    r.raise_for_status()
    return {a["name"]: a["id"] for a in r.json()}

def delete_asset(asset_id):
    requests.delete(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/assets/{asset_id}",
        headers=gh_headers()
    )

def upload_asset(upload_url, path):
    url = re.sub(r"\{.*?\}", "", upload_url)
    h = {**gh_headers(), "Content-Type": "application/vnd.android.package-archive"}
    with open(path, "rb") as f:
        r = requests.post(url, headers=h,
                          params={"name": path.name}, data=f, timeout=600)
    if r.status_code in (200, 201):
        dl = r.json().get("browser_download_url", "")
        print(f"    uploaded -> {dl}")
        return dl
    print(f"    upload failed {r.status_code}: {r.text[:200]}")
    return ""


# ── main ───────────────────────────────────────────────────────────────────

def try_sources(app):
    for name, fn in [("APKMirror", apkmirror),
                     ("Uptodown",  uptodown),
                     ("APKCombo",  apkcombo)]:
        try:
            result = fn(app)
            if result and result.exists():
                print(f"  -> SUCCESS via {name}")
                return result, name
        except Exception as e:
            print(f"  -> {name} exception: {e}")
        time.sleep(2)
    return None, None


def main():
    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN not set")
        sys.exit(1)

    apps = load_apps()
    print(f"[*] {len(apps)} apps")

    release_id, upload_url = get_or_create_release()
    print(f"[*] release_id={release_id}")
    existing = list_assets(release_id)
    print(f"[*] existing assets={len(existing)}")

    results = []
    for app in apps:
        name    = app["name"]
        package = app["package"]
        version = app.get("version", "latest")
        print(f"\n{'='*60}")
        print(f"[APP] {name} | {package} | v{version}")
        print(f"{'='*60}")

        apk, source = try_sources(app)

        if not apk:
            print(f"  FAILED all sources")
            results.append({"name": name, "status": "FAILED", "source": None, "url": ""})
            time.sleep(2)
            continue

        if apk.name in existing:
            print(f"  replacing old asset {apk.name}")
            delete_asset(existing[apk.name])

        dl_url = upload_asset(upload_url, apk)
        results.append({
            "name": name, "package": package, "version": version,
            "source": source,
            "status": "OK" if dl_url else "UPLOAD_FAILED",
            "url": dl_url
        })
        apk.unlink(missing_ok=True)
        time.sleep(3)

    with open("scrape_results.json", "w") as f:
        json.dump(results, f, indent=2)

    ok     = [r for r in results if r["status"] == "OK"]
    failed = [r["name"] for r in results if r["status"] != "OK"]
    print(f"\n[SUMMARY] OK={len(ok)}/{len(results)}")
    if failed:
        print(f"[FAILED] {', '.join(failed)}")
        sys.exit(1)
    print("[DONE]")


if __name__ == "__main__":
    main()
