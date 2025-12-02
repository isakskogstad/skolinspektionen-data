"""Data fetcher for downloading files from Skolinspektionen.

Downloads Excel files, PDFs and other data files for local processing.
Supports incremental updates and maintains a download manifest.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from ..config import get_settings
from .rate_limiter import extract_domain, get_rate_limiter

logger = logging.getLogger(__name__)

# Security: Allowed domains for downloads (SSRF protection)
ALLOWED_DOMAINS = frozenset([
    "skolinspektionen.se",
    "www.skolinspektionen.se",
])

# Security: Allowed download categories
ALLOWED_CATEGORIES = frozenset([
    "skolenkaten",
    "tillstand",
    "tillsyn",
    "tillsyn/viten",
    "tillsyn/tui",
    "tillsyn/planerad_tillsyn",
    "ombedomning",
    "publications",
])

# Security: Maximum file size (100 MB)
MAX_FILE_SIZE = 100 * 1024 * 1024

# Security: Allowed content types for downloads
ALLOWED_CONTENT_TYPES = frozenset([
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
    "application/vnd.ms-excel",  # .xls
    "application/pdf",
    "application/msword",  # .doc
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/octet-stream",  # Generic binary (some servers use this)
    "text/html",  # For web pages
    "text/plain",  # For text files
])


def validate_url(url: str, base_url: str) -> str:
    """Validate URL is from allowed domain (SSRF protection).

    Args:
        url: URL to validate (relative or absolute)
        base_url: Base URL to use for relative URLs

    Returns:
        Validated absolute URL

    Raises:
        ValueError: If URL is not from allowed domain
    """
    # Convert relative to absolute
    full_url = url if url.startswith("http") else urljoin(base_url, url)
    parsed = urlparse(full_url)

    # Block non-HTTP(S) schemes
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme}")

    # Block private IPs and localhost
    hostname = parsed.hostname or ""
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise ValueError("Private IPs not allowed")

    # Block private IP ranges (RFC 1918 + link-local + AWS metadata)
    if hostname.startswith("169.254"):  # Link-local / AWS metadata
        raise ValueError("Link-local addresses blocked")
    if hostname.startswith("10."):  # 10.0.0.0/8
        raise ValueError("Private IP range blocked")
    if hostname.startswith("192.168."):  # 192.168.0.0/16
        raise ValueError("Private IP range blocked")
    # 172.16.0.0 - 172.31.255.255 (172.16/12)
    if hostname.startswith("172."):
        try:
            second_octet = int(hostname.split(".")[1])
            if 16 <= second_octet <= 31:
                raise ValueError("Private IP range blocked")
        except (IndexError, ValueError):
            pass

    # Whitelist allowed domains
    if not any(hostname == domain or hostname.endswith("." + domain) for domain in ALLOWED_DOMAINS):
        raise ValueError(f"Domain not allowed: {hostname}")

    return full_url


def validate_category(category: str) -> str:
    """Validate download category (path traversal protection).

    Args:
        category: Category string

    Returns:
        Validated category

    Raises:
        ValueError: If category is invalid
    """
    # Remove any path traversal attempts
    clean_category = category.replace("..", "").strip("/")

    # Check against whitelist
    if clean_category not in ALLOWED_CATEGORIES:
        raise ValueError(f"Invalid category: {category}")

    return clean_category


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal.

    Args:
        filename: Original filename

    Returns:
        Sanitized filename safe for filesystem
    """
    # Get only the base filename (no path components)
    filename = os.path.basename(filename)

    # Remove null bytes
    filename = filename.replace("\x00", "")

    # Allow only safe characters: alphanumeric, dash, underscore, dot
    filename = re.sub(r"[^\w\-_.]", "_", filename)

    # Prevent hidden files
    if filename.startswith("."):
        filename = "_" + filename

    # Ensure not empty
    if not filename or filename in (".", ".."):
        filename = "unnamed_file"

    # Limit length
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        filename = name[:255-len(ext)] + ext

    return filename


class DownloadManifest:
    """Tracks downloaded files and their metadata."""

    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path
        self.entries: dict[str, dict] = {}
        self._load()

    def _load(self):
        """Load manifest from disk."""
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.entries = data.get("files", {})
            except Exception as e:
                logger.warning(f"Failed to load manifest: {e}")
                self.entries = {}

    def save(self):
        """Save manifest to disk."""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "last_updated": datetime.now().isoformat(),
                    "files": self.entries,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    def get_entry(self, url: str) -> Optional[dict]:
        """Get manifest entry for a URL."""
        return self.entries.get(url)

    def update_entry(
        self,
        url: str,
        local_path: str,
        content_hash: str,
        size: int,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ):
        """Update or create manifest entry."""
        self.entries[url] = {
            "local_path": local_path,
            "content_hash": content_hash,
            "size": size,
            "etag": etag,
            "last_modified": last_modified,
            "downloaded_at": datetime.now().isoformat(),
        }

    def needs_update(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        content_length: Optional[int] = None,
    ) -> bool:
        """Check if a file needs to be re-downloaded."""
        entry = self.get_entry(url)
        if not entry:
            return True

        # Check if local file still exists
        local_path = Path(entry["local_path"])
        if not local_path.exists():
            return True

        # Compare etag if available
        if etag and entry.get("etag") and etag != entry["etag"]:
            return True

        # Compare last-modified if available
        if last_modified and entry.get("last_modified") and last_modified != entry["last_modified"]:
            return True

        # Compare content length if available
        if content_length and entry.get("size") and content_length != entry["size"]:
            return True

        return False


# Known file URLs for Skolinspektionen data sources
SKOLENKATEN_URLS = {
    # Base patterns - actual files discovered dynamically
    "base_path": "/globalassets/02-beslut-rapporter-stat/statistik/statistik-skolenkaten/",
    "years": range(2015, 2026),
    "respondent_types": [
        "elever-grundskola-ak-5",
        "elever-grundskola-ak-8",
        "elever-gymnasieskola-ar-2",
        "larare-grundskola-ak-1-9",
        "larare-gymnasieskola",
        "vardnadshavare-forskoleklass",
        "vardnadshavare-grundskola-ak-1-9",
        "vardnadshavare-anpassad-grundskola",
        "pedagogisk-personal-forskola",
        "vardnadshavare-forskola",
    ],
}

TILLSTAND_URLS = {
    "base_path": "/globalassets/02-beslut-rapporter-stat/statistik/statistik-tillstand/",
    "years": range(2018, 2026),
}

TILLSYN_URLS = {
    "viten": "/globalassets/02-beslut-rapporter-stat/statistik/statistik-viten/viten-historik.xlsx",
    "planerad_tillsyn_base": "/globalassets/02-beslut-rapporter-stat/statistik/planerad-tillsyn/",
    "tui_base": "/globalassets/02-beslut-rapporter-stat/statistik/rt-individ/",
}


class DataFetcher:
    """Downloads and manages data files from Skolinspektionen."""

    def __init__(
        self,
        download_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ):
        self.settings = get_settings()
        self.download_dir = download_dir or (self.settings.data_dir / "downloads")
        self.timeout = timeout
        self.rate_limiter = get_rate_limiter()
        self.client: Optional[httpx.AsyncClient] = None
        self.manifest = DownloadManifest(self.download_dir / "manifest.json")

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": self.settings.user_agent},
        )
        return self

    async def __aexit__(self, *args):
        if self.client:
            await self.client.aclose()
        self.manifest.save()

    def _get_local_path(self, url: str, category: str) -> Path:
        """Generate local path for a downloaded file.

        Args:
            url: URL to derive filename from
            category: Validated category folder

        Returns:
            Safe local path within download_dir

        Raises:
            ValueError: If path would escape download_dir
        """
        parsed = urlparse(url)
        raw_filename = Path(parsed.path).name

        # Sanitize filename to prevent path traversal
        safe_filename = sanitize_filename(raw_filename)

        # Construct path
        local_path = self.download_dir / category / safe_filename

        # Final safety check: ensure path is within download_dir
        resolved = local_path.resolve()
        download_resolved = self.download_dir.resolve()
        if not str(resolved).startswith(str(download_resolved)):
            raise ValueError(f"Path traversal detected: {local_path}")

        return local_path

    async def _check_file_headers(self, url: str) -> dict:
        """Get file headers without downloading."""
        try:
            full_url = validate_url(url, self.settings.base_url)
        except ValueError as e:
            logger.debug(f"URL validation failed for {url}: {e}")
            return {"exists": False, "error": str(e)}

        try:
            domain = extract_domain(full_url)
            async with self.rate_limiter.limit(domain):
                response = await self.client.head(full_url)
                if response.status_code == 200:
                    return {
                        "exists": True,
                        "etag": response.headers.get("etag"),
                        "last_modified": response.headers.get("last-modified"),
                        "content_length": int(response.headers.get("content-length", 0)),
                        "content_type": response.headers.get("content-type"),
                    }
                return {"exists": False, "status": response.status_code}
        except httpx.HTTPError as e:
            logger.debug(f"HEAD request failed for {url}: {e}")
            return {"exists": False, "error": str(e)}
        except Exception as e:
            logger.debug(f"Unexpected error checking {url}: {e}")
            return {"exists": False, "error": str(e)}

    async def download_file(
        self,
        url: str,
        category: str,
        force: bool = False,
    ) -> Optional[Path]:
        """Download a file if needed.

        Args:
            url: URL to download (relative or absolute)
            category: Category folder (skolenkaten, tillstand, etc.)
            force: Force re-download even if file exists

        Returns:
            Local path to downloaded file, or None if failed
        """
        try:
            # Security: Validate URL (SSRF protection)
            full_url = validate_url(url, self.settings.base_url)

            # Security: Validate category (path traversal protection)
            safe_category = validate_category(category)

            # Get safe local path
            local_path = self._get_local_path(url, safe_category)
        except ValueError as e:
            logger.error(f"Security validation failed for {url}: {e}")
            return None

        # Check if update needed
        if not force:
            headers = await self._check_file_headers(full_url)
            if not headers.get("exists"):
                logger.debug(f"File not found: {full_url}")
                return None

            # Security: Check file size before download
            content_length = headers.get("content_length", 0)
            if content_length > MAX_FILE_SIZE:
                logger.error(f"File too large ({content_length} bytes): {full_url}")
                return None

            if not self.manifest.needs_update(
                url,
                etag=headers.get("etag"),
                last_modified=headers.get("last_modified"),
                content_length=content_length,
            ):
                logger.debug(f"File up to date: {local_path}")
                return local_path

        # Download file
        try:
            domain = extract_domain(full_url)
            async with self.rate_limiter.limit(domain):
                response = await self.client.get(full_url)
                response.raise_for_status()

                # Security: Validate content-type
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                if content_type and content_type not in ALLOWED_CONTENT_TYPES:
                    logger.error(f"Blocked content-type '{content_type}' for: {full_url}")
                    return None

                content = response.content

                # Security: Verify file size after download
                if len(content) > MAX_FILE_SIZE:
                    logger.error(f"Downloaded file too large ({len(content)} bytes): {full_url}")
                    return None

                # Save file
                local_path.parent.mkdir(parents=True, exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(content)

                # Update manifest
                content_hash = hashlib.sha256(content).hexdigest()
                self.manifest.update_entry(
                    url=url,
                    local_path=str(local_path),
                    content_hash=content_hash,
                    size=len(content),
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )

                logger.info(f"Downloaded: {local_path.name} ({len(content)} bytes)")
                return local_path

        except httpx.HTTPError as e:
            logger.error(f"HTTP error downloading {full_url}: {e}")
            return None
        except (IOError, OSError) as e:
            logger.error(f"File system error saving {local_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error downloading {full_url}: {e}")
            return None

    async def discover_skolenkaten_files(self) -> list[str]:
        """Discover available Skolenkäten Excel files."""
        discovered = []
        base = SKOLENKATEN_URLS["base_path"]

        for year in SKOLENKATEN_URLS["years"]:
            for resp_type in SKOLENKATEN_URLS["respondent_types"]:
                # Try common filename patterns
                patterns = [
                    f"{base}{year}/{resp_type}.xlsx",
                    f"{base}{year}/{resp_type}-vt{year}.xlsx",
                    f"{base}{year}/{resp_type}-ht{year}.xlsx",
                    f"{base}{year}/vt-{year}/{resp_type}.xlsx",
                    f"{base}{year}/ht-{year}/{resp_type}.xlsx",
                ]

                for pattern in patterns:
                    headers = await self._check_file_headers(pattern)
                    if headers.get("exists"):
                        discovered.append(pattern)
                        break  # Found one pattern, skip others

        return discovered

    async def discover_tillstand_files(self) -> list[str]:
        """Discover available Tillståndsbeslut Excel files."""
        discovered = []
        base = TILLSTAND_URLS["base_path"]

        for year in TILLSTAND_URLS["years"]:
            # Try common patterns
            patterns = [
                f"{base}{year}-skolstart-{year+1}-{str(year+2)[-2:]}/tillstandsbeslut-{year}.xlsx",
                f"{base}{year}-skolstart-{year+1}-{str(year+2)[-2:]}/tillstandsbeslut-{year}-publicering.xlsx",
                f"{base}{year}/tillstandsbeslut-{year}.xlsx",
            ]

            for pattern in patterns:
                headers = await self._check_file_headers(pattern)
                if headers.get("exists"):
                    discovered.append(pattern)
                    break

        return discovered

    async def discover_tillsyn_files(self) -> dict[str, list[str]]:
        """Discover available Tillsyn statistics files."""
        discovered = {"viten": [], "tui": [], "planerad_tillsyn": []}

        # Check viten
        viten_url = TILLSYN_URLS["viten"]
        headers = await self._check_file_headers(viten_url)
        if headers.get("exists"):
            discovered["viten"].append(viten_url)

        # Check TUI/RT-individ files
        tui_base = TILLSYN_URLS["tui_base"]
        for year in range(2020, 2026):
            patterns = [
                f"{tui_base}{year}/rt-individ-{year}.xlsx",
                f"{tui_base}{year}/statistik-riktad-tillsyn-individ-{year}.xlsx",
                f"{tui_base}rt-{year}-individ/statistik-riktad-tillsyn-individ-{year}.xlsx",
            ]
            for pattern in patterns:
                headers = await self._check_file_headers(pattern)
                if headers.get("exists"):
                    discovered["tui"].append(pattern)
                    break

        # Check Planerad tillsyn files
        pt_base = TILLSYN_URLS["planerad_tillsyn_base"]
        for year in range(2020, 2026):
            patterns = [
                f"{pt_base}{year}/planerad-tillsyn-{year}.xlsx",
                f"{pt_base}pt-{year}/statistik-planerad-tillsyn-{year}.xlsx",
                f"{pt_base}{year}/arsstatistik-{year}.xlsx",
            ]
            for pattern in patterns:
                headers = await self._check_file_headers(pattern)
                if headers.get("exists"):
                    discovered["planerad_tillsyn"].append(pattern)
                    break

        return discovered

    async def fetch_all_skolenkaten(self, force: bool = False) -> list[Path]:
        """Download all Skolenkäten files."""
        urls = await self.discover_skolenkaten_files()
        logger.info(f"Found {len(urls)} Skolenkäten files")

        downloaded = []
        for url in urls:
            path = await self.download_file(url, "skolenkaten", force=force)
            if path:
                downloaded.append(path)
            await asyncio.sleep(0.5)  # Respectful delay

        return downloaded

    async def fetch_all_tillstand(self, force: bool = False) -> list[Path]:
        """Download all Tillståndsbeslut files."""
        urls = await self.discover_tillstand_files()
        logger.info(f"Found {len(urls)} Tillstånd files")

        downloaded = []
        for url in urls:
            path = await self.download_file(url, "tillstand", force=force)
            if path:
                downloaded.append(path)
            await asyncio.sleep(0.5)

        return downloaded

    async def fetch_all_tillsyn(self, force: bool = False) -> dict[str, list[Path]]:
        """Download all Tillsyn statistics files."""
        urls = await self.discover_tillsyn_files()

        downloaded = {"viten": [], "tui": [], "planerad_tillsyn": []}

        for category, url_list in urls.items():
            logger.info(f"Found {len(url_list)} {category} files")
            for url in url_list:
                path = await self.download_file(url, f"tillsyn/{category}", force=force)
                if path:
                    downloaded[category].append(path)
                await asyncio.sleep(0.5)

        return downloaded

    def get_download_stats(self) -> dict:
        """Get statistics about downloaded files."""
        stats = {
            "total_files": len(self.manifest.entries),
            "by_category": {},
            "total_size_bytes": 0,
            "last_updated": None,
        }

        for url, entry in self.manifest.entries.items():
            local_path = Path(entry["local_path"])
            category = local_path.parent.name

            if category not in stats["by_category"]:
                stats["by_category"][category] = {"count": 0, "size": 0}

            stats["by_category"][category]["count"] += 1
            stats["by_category"][category]["size"] += entry.get("size", 0)
            stats["total_size_bytes"] += entry.get("size", 0)

            downloaded_at = entry.get("downloaded_at")
            if downloaded_at:
                if not stats["last_updated"] or downloaded_at > stats["last_updated"]:
                    stats["last_updated"] = downloaded_at

        return stats
