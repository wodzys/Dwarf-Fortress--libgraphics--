#!/usr/bin/env python3
"""
Track Dwarf Fortress Linux g_src versions.

Parses https://www.bay12games.com/dwarves/older_versions.html,
downloads new DF Linux releases, extracts g_src/ plus documentation
files (file changes.txt, release notes.txt), and creates a git commit
with an annotated tag for each version.

Baseline: v51.05 — versions before this are not tracked.

Usage:
    python scripts/track_versions.py              # Auto-discover new versions
    python scripts/track_versions.py 51_05        # Process a specific version
    REQUESTED_VERSION=51_05 python scripts/track_versions.py  # Via env var
"""

import os
import re
import sys
import time
import shutil
import tarfile
import logging
import tempfile
import subprocess
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OLDER_VERSIONS_URL = "https://www.bay12games.com/dwarves/older_versions.html"
BASE_URL = "https://www.bay12games.com/dwarves/"
BASELINE = (51, 5)  # v51.05 — versions before this are ignored
LINUX_LINK_RE = re.compile(
    r'href="([^"]*df_(\d+)_(\d+)_linux\.tar\.(?:bz2|gz))"'
)
REPO_ROOT = Path(__file__).resolve().parent.parent
G_SRC_DIR = REPO_ROOT / "g_src"

# Canonical documentation file names in the archive
DOC_FILES = [
    "file changes.txt",
    "release notes.txt",
]
# Alternative (underscored) names also checked
ALT_DOC_FILES = [
    "file_changes.txt",
    "release_notes.txt",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("track_versions")


# ---------------------------------------------------------------------------
# Web helpers
# ---------------------------------------------------------------------------

def _http_headers():
    return {
        "User-Agent": "DF-libgraphics-tracker/1.0 (+https://github.com/wodzys/Dwarf-Fortress--libgraphics--)"
    }


def fetch_page(url: str, retries: int = 3) -> str:
    """Fetch a web page with retries and exponential backoff."""
    for attempt in range(retries):
        try:
            req = Request(url, headers=_http_headers())
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (URLError, HTTPError) as e:
            log.warning("Fetch attempt %d/%d for %s failed: %s", attempt + 1, retries, url, e)
            if attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts")


def download_file(url: str, dest: Path, retries: int = 3) -> bool:
    """Download a file to *dest* with retries.  Returns True on success."""
    for attempt in range(retries):
        try:
            log.info("Downloading %s", url)
            req = Request(url, headers=_http_headers())
            with urlopen(req, timeout=120) as resp:
                data = resp.read()
            size_mb = len(data) / (1024 * 1024)
            dest.write_bytes(data)
            log.info("Downloaded %.1f MB → %s", size_mb, dest.name)
            return True
        except (URLError, HTTPError) as e:
            log.warning("Download attempt %d/%d failed: %s", attempt + 1, retries, e)
            if attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
    return False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_linux_links(html: str) -> list[tuple[str, str, int, int]]:
    """Parse 64-bit Linux download links from the older-versions HTML page.

    Returns a list of ``(version_key, url, major, minor)`` tuples sorted by
    version.  *version_key* is e.g. ``"51_05"``.
    """
    raw: list[tuple[str, str, int, int]] = []
    for m in LINUX_LINK_RE.finditer(html):
        href = m.group(1)
        major = int(m.group(2))
        minor = int(m.group(3))

        # Skip 32-bit builds
        if "_linux32" in href:
            continue

        # Baseline filter
        if (major, minor) < BASELINE:
            continue

        version_key = f"{major}_{minor:02d}"
        url = href if href.startswith("http") else BASE_URL + href
        raw.append((version_key, url, major, minor))

    # Deduplicate by version_key (keep first occurrence)
    seen: dict[str, tuple[str, int, int]] = {}
    for vk, url, major, minor in raw:
        if vk not in seen:
            seen[vk] = (url, major, minor)

    return [(vk, seen[vk][0], seen[vk][1], seen[vk][2]) for vk in sorted(seen)]


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def run_git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command from the repo root."""
    return subprocess.run(
        ["git"] + args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


def get_existing_tags() -> set[str]:
    """Return the set of version keys that already have a ``v*`` tag."""
    try:
        output = run_git(["tag", "-l", "v*"]).stdout.strip()
    except subprocess.CalledProcessError:
        log.error("Failed to list git tags")
        return set()

    if not output:
        return set()

    tags: set[str] = set()
    for line in output.splitlines():
        tag = line.strip()
        if tag.startswith("v"):
            # tag like "v51_05" → version_key "51_05"
            tags.add(tag[1:])
    return tags


def configure_git_author():
    """Set git user name / email for the CI bot."""
    run_git(["config", "user.name", "github-actions[bot]"], check=False)
    run_git(
        ["config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=False,
    )


# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------

def _find_df_linux_dir(extract_root: Path, archive_path: Path) -> Path:
    """Locate the ``df_linux/`` directory inside an extracted archive."""
    # Direct child
    candidate = extract_root / "df_linux"
    if candidate.is_dir():
        return candidate

    # The archive may use a different top-level directory name.
    # Read the member list to find the common prefix.
    with tarfile.open(archive_path, "r:*") as tar:
        members = [m.name for m in tar.getmembers() if not m.isdir()]
    if members:
        prefix = members[0].split("/")[0]
        candidate = extract_root / prefix
        if candidate.is_dir():
            return candidate

    raise FileNotFoundError(
        f"Cannot locate df_linux/ directory inside {archive_path.name}"
    )


def extract_and_replace(archive_path: Path, extract_dir: Path) -> list[str]:
    """Extract g_src and documentation from *archive_path*, replacing local files.

    Returns the list of documentation file names that were copied.
    """
    log.info("Extracting %s → %s", archive_path.name, extract_dir)

    # Python ≥3.12 filter='data' prevents path-traversal attacks
    with tarfile.open(archive_path, "r:*") as tar:
        tar.extractall(path=extract_dir, filter="data")

    df_linux = _find_df_linux_dir(extract_dir, archive_path)
    log.info("Found df_linux at %s", df_linux)

    g_src_archive = df_linux / "g_src"
    if not g_src_archive.is_dir():
        raise FileNotFoundError(f"g_src/ not found inside {df_linux}")

    # ---- Replace g_src/ ---------------------------------------------------
    g_src_count_before = 0
    if G_SRC_DIR.exists():
        g_src_count_before = len(list(G_SRC_DIR.rglob("*")))
    log.info("Replacing g_src/ (%d files before) …", g_src_count_before)
    if G_SRC_DIR.exists():
        shutil.rmtree(G_SRC_DIR)
    shutil.copytree(g_src_archive, G_SRC_DIR)
    g_src_count_after = len(list(G_SRC_DIR.rglob("*")))
    log.info("g_src/ replaced: %d → %d files", g_src_count_before, g_src_count_after)

    # ---- Copy documentation files -----------------------------------------
    copied: list[str] = []
    for canonical, alt in zip(DOC_FILES, ALT_DOC_FILES):
        src = df_linux / canonical
        if not src.exists():
            src = df_linux / alt
        if src.exists():
            dest = REPO_ROOT / canonical
            shutil.copy2(src, dest)
            copied.append(canonical)
            log.info("Copied %s", canonical)
        else:
            log.warning("Documentation file not found: %s (or %s)", canonical, alt)

    return copied


# ---------------------------------------------------------------------------
# Commit + tag
# ---------------------------------------------------------------------------

def git_commit_and_tag(
    version_key: str,
    version_display: str,
    download_url: str,
) -> bool:
    """Stage changes, commit (if any), and create an annotated tag.

    Returns True if a commit was made.
    """
    # Stage relevant paths
    paths_to_stage = ["g_src/", *DOC_FILES, *ALT_DOC_FILES]
    for p in paths_to_stage:
        run_git(["add", "-A", p], check=False)

    # Check for staged changes
    changed_files = run_git(["diff", "--cached", "--name-only"], check=False).stdout.strip()
    if not changed_files:
        log.info("No changes for %s — skipping commit", version_display)
        return False

    log.info("Changed files:\n%s", changed_files)

    commit_msg = version_display
    run_git(["commit", "-m", commit_msg])
    commit_hash = run_git(["rev-parse", "HEAD"]).stdout.strip()[:8]
    log.info("Committed: %s  (hash: %s)", commit_msg, commit_hash)

    tag_name = f"v{version_key}"
    tag_msg = f"DF {version_display}\n\nSource: {download_url}"
    run_git(["tag", "-a", tag_name, "-m", tag_msg])
    log.info("Tagged: %s  →  DF %s", tag_name, version_display)
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Resolve requested version from CLI arg or env var
    requested_version = ""
    if len(sys.argv) > 1:
        requested_version = sys.argv[1].strip()
    if not requested_version:
        requested_version = os.environ.get("REQUESTED_VERSION", "").strip()

    log.info("=== DF libgraphics Version Tracker ===")
    log.info("Baseline: v%d.%02d", BASELINE[0], BASELINE[1])

    configure_git_author()

    # 1. Fetch and parse the versions page
    log.info("Fetching %s …", OLDER_VERSIONS_URL)
    try:
        html = fetch_page(OLDER_VERSIONS_URL)
    except RuntimeError as e:
        log.error("%s", e)
        sys.exit(1)

    all_links = parse_linux_links(html)
    log.info(
        "Found %d Linux versions ≥ v%d.%02d",
        len(all_links),
        BASELINE[0],
        BASELINE[1],
    )

    if not all_links:
        log.warning("No matching Linux versions on the page — nothing to do")
        return

    # Print all found versions for visibility
    log.info("Versions found on page (≥ baseline):")
    for vk, url, major, minor in all_links:
        log.info("  v%s  →  %s", vk, url)

    # 2. Determine which versions to process
    existing_tags = get_existing_tags()
    log.info("Existing tags in repo: %d", len(existing_tags))
    if existing_tags:
        for t in sorted(existing_tags):
            log.info("  tagged: v%s", t)

    to_process: list[tuple[str, str, int, int]] = []
    if requested_version:
        for vk, url, major, minor in all_links:
            if vk == requested_version:
                to_process.append((vk, url, major, minor))
                break
        if not to_process:
            log.error(
                "Requested version '%s' not found on the versions page "
                "(or is below baseline v%d.%02d)",
                requested_version,
                BASELINE[0],
                BASELINE[1],
            )
            sys.exit(1)
    else:
        for vk, url, major, minor in all_links:
            if vk not in existing_tags:
                to_process.append((vk, url, major, minor))
            else:
                log.info("  v%s — already tagged, skipping", vk)

    if not to_process:
        log.info("No new versions to process — up to date!")
        return

    log.info(
        "Will process %d version(s): %s",
        len(to_process),
        ", ".join(vk for vk, _, _, _ in to_process),
    )

    # 3. Process each version
    temp_base = Path(tempfile.mkdtemp(prefix="df_tracker_"))
    processed: list[str] = []
    start_time = time.time()
    try:
        for idx, (version_key, download_url, major, minor) in enumerate(to_process, 1):
            version_display = f"{major}.{minor:02d}"
            v_start = time.time()
            log.info("")
            log.info("─── [%d/%d] Processing DF %s ───", idx, len(to_process), version_display)

            # Download
            archive_name = f"df_{version_key}_linux.tar.bz2"
            archive_path = temp_base / archive_name
            if not download_file(download_url, archive_path):
                log.error("SKIPPED %s — download failed after retries", version_display)
                continue

            # Extract & replace
            extract_dir = temp_base / f"extract_{version_key}"
            extract_dir.mkdir(exist_ok=True)
            try:
                doc_files = extract_and_replace(archive_path, extract_dir)
                if not doc_files:
                    log.warning(
                        "No documentation files found in archive for %s",
                        version_display,
                    )
            except Exception as e:
                log.error("SKIPPED %s — extraction failed: %s", version_display, e)
                continue

            # Commit & tag
            try:
                git_commit_and_tag(version_key, version_display, download_url)
            except Exception as e:
                log.error("SKIPPED %s — git operation failed: %s", version_display, e)
                continue

            processed.append(version_key)
            v_elapsed = time.time() - v_start
            log.info("✓ DF %s done in %.1fs", version_display, v_elapsed)

            # Clean up extraction for this version (free disk space)
            shutil.rmtree(extract_dir, ignore_errors=True)
            archive_path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(temp_base, ignore_errors=True)

    # 4. Summary
    total_elapsed = time.time() - start_time
    log.info("")
    log.info("=== Done ===")
    if processed:
        log.info(
            "Successfully processed %d/%d version(s) in %.1fs: %s",
            len(processed),
            len(to_process),
            total_elapsed,
            processed,
        )
    else:
        log.warning("No versions were successfully processed")
        sys.exit(1)


if __name__ == "__main__":
    main()
