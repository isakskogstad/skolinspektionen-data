"""Parser for converting Skolinspektionen HTML content to Markdown."""

import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from rich.console import Console

from .models import Publication, Attachment

console = Console()

BASE_URL = "https://www.skolinspektionen.se"

# Security: Allowed domains for content fetching (SSRF protection)
ALLOWED_DOMAINS = frozenset([
    "skolinspektionen.se",
    "www.skolinspektionen.se",
])


def validate_url(url: str) -> str:
    """Validate URL is from allowed domain (SSRF protection).

    Args:
        url: URL to validate (relative or absolute)

    Returns:
        Validated absolute URL

    Raises:
        ValueError: If URL is not from allowed domain or uses blocked IPs
    """
    # Convert relative to absolute
    full_url = url if url.startswith("http") else BASE_URL + url
    parsed = urlparse(full_url)

    # Block non-HTTP(S) schemes
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme}")

    hostname = parsed.hostname or ""

    # Block localhost and loopback
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise ValueError("Private IPs not allowed")

    # Block private IP ranges (RFC 1918 + link-local + AWS metadata)
    if hostname.startswith("169.254"):  # Link-local
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


class ContentParser:
    """Parser for fetching and converting publication content to Markdown."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "SkolinspektionenData/0.1 (https://github.com/civictechsweden/skolinspektionen-data)"
            },
        )
        return self

    async def __aexit__(self, *args):
        if self.client:
            await self.client.aclose()

    async def fetch_publication_content(self, url: str) -> Optional[dict]:
        """
        Fetch a publication page and extract its content as Markdown.

        Returns a dict with:
        - title: Publication title
        - markdown: Main content as Markdown
        - attachments: List of PDF/Excel attachments
        - metadata: Additional metadata (published date, diarienummer, etc.)
        """
        # Security: Validate URL before fetching (SSRF protection)
        try:
            validated_url = validate_url(url)
        except ValueError as e:
            console.print(f"[red]URL validation failed for {url}: {e}[/red]")
            return None

        try:
            response = await self.client.get(validated_url)
            response.raise_for_status()
            html = response.text
        except httpx.HTTPError as e:
            console.print(f"[red]Error fetching {validated_url}: {e}[/red]")
            return None

        return self.parse_publication_page(html, validated_url)

    def parse_publication_page(self, html: str, source_url: str) -> dict:
        """Parse a publication page HTML into structured content."""
        soup = BeautifulSoup(html, "html.parser")

        # Extract title
        title = self._extract_title(soup)

        # Extract main content
        content_elem = self._find_main_content(soup)
        markdown = self._convert_to_markdown(content_elem) if content_elem else ""

        # Extract attachments (PDFs, Excel files)
        attachments = self._extract_attachments(soup)

        # Extract metadata
        metadata = self._extract_metadata(soup)

        return {
            "title": title,
            "markdown": markdown,
            "attachments": attachments,
            "metadata": metadata,
            "source_url": source_url,
        }

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract the page title."""
        # Try various common title locations
        selectors = [
            "h1",
            "article h1",
            ".page-title",
            ".article-title",
            "[class*='title'] h1",
        ]

        for selector in selectors:
            elem = soup.select_one(selector)
            if elem:
                return elem.get_text(strip=True)

        # Fall back to page title
        title_tag = soup.find("title")
        if title_tag:
            return title_tag.get_text(strip=True).split("|")[0].strip()

        return "Untitled"

    def _find_main_content(self, soup: BeautifulSoup) -> Optional[BeautifulSoup]:
        """Find the main content container."""
        # Try common content selectors for Swedish government sites
        selectors = [
            "article",
            "main",
            ".main-content",
            ".article-content",
            ".page-content",
            "[class*='content']",
            "#content",
        ]

        for selector in selectors:
            elem = soup.select_one(selector)
            if elem and len(elem.get_text(strip=True)) > 100:
                return elem

        # Fall back to body
        return soup.find("body")

    def _convert_to_markdown(self, elem: BeautifulSoup) -> str:
        """Convert HTML element to clean Markdown."""
        if not elem:
            return ""

        # Remove unwanted elements
        for unwanted in elem.select("script, style, nav, footer, header, .menu, .navigation"):
            unwanted.decompose()

        # Convert to markdown
        markdown = md(
            str(elem),
            heading_style="ATX",
            bullets="-",
            strip=["a"],  # Remove empty links
        )

        # Clean up the markdown
        markdown = self._clean_markdown(markdown)

        return markdown

    def _clean_markdown(self, text: str) -> str:
        """Clean up markdown text."""
        # Remove excessive newlines
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Remove leading/trailing whitespace from lines
        lines = [line.strip() for line in text.split("\n")]
        text = "\n".join(lines)

        # Remove empty headers
        text = re.sub(r"^#+\s*$", "", text, flags=re.MULTILINE)

        return text.strip()

    def _extract_attachments(self, soup: BeautifulSoup) -> list[Attachment]:
        """Extract PDF and Excel attachments."""
        attachments = []

        # Find all downloadable file links
        file_extensions = [".pdf", ".xlsx", ".xls", ".doc", ".docx"]

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")

            for ext in file_extensions:
                if ext in href.lower():
                    name = link.get_text(strip=True) or f"Attachment{ext}"
                    url = href if href.startswith("http") else urljoin(BASE_URL, href)

                    # Determine file type
                    file_type = ext.lstrip(".")
                    if file_type in ["xls", "xlsx"]:
                        file_type = "excel"
                    elif file_type in ["doc", "docx"]:
                        file_type = "word"

                    attachments.append(
                        Attachment(
                            name=name,
                            url=url,
                            file_type=file_type,
                        )
                    )
                    break

        # Deduplicate by URL
        seen_urls = set()
        unique_attachments = []
        for att in attachments:
            if att.url not in seen_urls:
                seen_urls.add(att.url)
                unique_attachments.append(att)

        return unique_attachments

    def _extract_metadata(self, soup: BeautifulSoup) -> dict:
        """Extract metadata from the page."""
        metadata = {}

        # Look for common metadata patterns
        # Diarienummer
        for pattern in [r"[Dd]iarienummer[:\s]+([A-Z0-9-]+)", r"[Dd]nr[:\s]+([A-Z0-9-]+)"]:
            match = re.search(pattern, soup.get_text())
            if match:
                metadata["diarienummer"] = match.group(1)
                break

        # Publication date
        date_elem = soup.select_one("time, .date, [class*='published'], [class*='date']")
        if date_elem:
            metadata["published"] = date_elem.get("datetime") or date_elem.get_text(strip=True)

        # Categories/themes
        theme_links = soup.select("a[href*='/teman/'], .theme, .category")
        if theme_links:
            metadata["themes"] = [link.get_text(strip=True) for link in theme_links]

        return metadata

    async def fetch_press_release_content(self, url: str) -> Optional[dict]:
        """Fetch a press release page and extract its content."""
        return await self.fetch_publication_content(url)

    async def get_full_publication(self, publication: Publication) -> dict:
        """
        Get full publication content including Markdown text.

        Enhances a Publication object with full content.
        """
        content = await self.fetch_publication_content(publication.url)

        if not content:
            return {
                "publication": publication.model_dump(mode="json"),
                "content": None,
                "error": "Failed to fetch content",
            }

        # Merge any new attachments found
        all_attachments = list(publication.attachments)
        for att in content.get("attachments", []):
            if att.url not in [a.url for a in all_attachments]:
                all_attachments.append(att)

        return {
            "publication": publication.model_dump(mode="json"),
            "markdown": content["markdown"],
            "attachments": [a.model_dump(mode="json") for a in all_attachments],
            "metadata": content["metadata"],
        }
