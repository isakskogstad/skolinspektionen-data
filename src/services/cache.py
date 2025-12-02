"""Two-tier caching system with memory (LRU) and disk storage.

Provides fast access to frequently used content while persisting
data for longer-term caching.
"""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Generic, Optional, TypeVar

import aiofiles
import aiofiles.os
from rich.console import Console

from ..config import get_settings

console = Console()

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    """A cache entry with value and metadata."""

    value: T
    created_at: float
    ttl_seconds: float
    hits: int = 0

    @property
    def is_expired(self) -> bool:
        """Check if the entry has expired."""
        return time.time() > self.created_at + self.ttl_seconds

    @property
    def age_seconds(self) -> float:
        """Get age of entry in seconds."""
        return time.time() - self.created_at

    @property
    def expires_at(self) -> datetime:
        """Get expiration datetime."""
        return datetime.fromtimestamp(self.created_at + self.ttl_seconds)


class LRUCache(Generic[T]):
    """In-memory LRU (Least Recently Used) cache.

    Fast access for frequently used items with automatic eviction
    of least recently used items when capacity is exceeded.
    """

    def __init__(self, max_size: int = 50):
        """Initialize LRU cache.

        Args:
            max_size: Maximum number of items to store
        """
        self.max_size = max_size
        self._cache: OrderedDict[str, CacheEntry[T]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[T]:
        """Get a value from cache, returning None if not found or expired."""
        async with self._lock:
            if key not in self._cache:
                return None

            entry = self._cache[key]

            if entry.is_expired:
                del self._cache[key]
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            entry.hits += 1
            return entry.value

    async def set(self, key: str, value: T, ttl_seconds: float) -> None:
        """Set a value in cache with TTL."""
        async with self._lock:
            # Remove if exists to update position
            if key in self._cache:
                del self._cache[key]

            # Evict oldest if at capacity
            while len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)

            self._cache[key] = CacheEntry(
                value=value,
                created_at=time.time(),
                ttl_seconds=ttl_seconds,
            )

    async def delete(self, key: str) -> bool:
        """Delete a key from cache. Returns True if key existed."""
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    async def clear(self) -> int:
        """Clear all items from cache. Returns count of items cleared."""
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    async def clear_expired(self) -> int:
        """Remove expired entries. Returns count of items removed."""
        async with self._lock:
            expired_keys = [key for key, entry in self._cache.items() if entry.is_expired]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)

    @property
    def size(self) -> int:
        """Current number of items in cache."""
        return len(self._cache)

    def get_stats(self) -> dict:
        """Get cache statistics."""
        total_hits = sum(entry.hits for entry in self._cache.values())
        return {
            "size": self.size,
            "max_size": self.max_size,
            "total_hits": total_hits,
            "entries": [
                {
                    "key": key,
                    "hits": entry.hits,
                    "age_seconds": entry.age_seconds,
                    "expires_at": entry.expires_at.isoformat(),
                }
                for key, entry in self._cache.items()
            ],
        }


class DiskCache:
    """Disk-based cache for persistent storage.

    Stores JSON-serializable data in files with TTL support.
    Uses content-addressable storage with hashed filenames.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        """Initialize disk cache.

        Args:
            cache_dir: Directory for cache files (uses settings default if not provided)
        """
        settings = get_settings()
        self.cache_dir = cache_dir or settings.effective_cache_dir
        self._lock = asyncio.Lock()

    def _key_to_path(self, key: str) -> Path:
        """Convert cache key to file path using content hash."""
        key_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self.cache_dir / f"{key_hash}.json"

    async def _ensure_dir(self) -> None:
        """Ensure cache directory exists."""
        try:
            await aiofiles.os.makedirs(self.cache_dir, exist_ok=True)
        except Exception:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def get(self, key: str) -> Optional[Any]:
        """Get a value from disk cache."""
        path = self._key_to_path(key)

        try:
            if not path.exists():
                return None

            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                content = await f.read()

            data = json.loads(content)

            # Check expiration
            expires_at = data.get("expires_at", 0)
            if time.time() > expires_at:
                # Expired, delete file
                try:
                    await aiofiles.os.remove(path)
                except Exception:
                    pass
                return None

            return data.get("value")

        except (json.JSONDecodeError, KeyError, IOError) as e:
            console.print(f"[dim]Disk cache read error for {key}: {e}[/dim]")
            return None

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """Set a value in disk cache with TTL."""
        await self._ensure_dir()

        path = self._key_to_path(key)
        data = {
            "key": key,
            "value": value,
            "created_at": time.time(),
            "expires_at": time.time() + ttl_seconds,
        }

        async with self._lock:
            try:
                async with aiofiles.open(path, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            except Exception as e:
                console.print(f"[yellow]Disk cache write error: {e}[/yellow]")

    async def delete(self, key: str) -> bool:
        """Delete a key from disk cache."""
        path = self._key_to_path(key)
        try:
            if path.exists():
                await aiofiles.os.remove(path)
                return True
        except Exception:
            pass
        return False

    async def clear(self) -> int:
        """Clear all cache files. Returns count of files removed."""
        if not self.cache_dir.exists():
            return 0

        count = 0
        for path in self.cache_dir.glob("*.json"):
            try:
                await aiofiles.os.remove(path)
                count += 1
            except Exception:
                pass
        return count

    async def clear_expired(self) -> int:
        """Remove expired cache files. Returns count of files removed."""
        if not self.cache_dir.exists():
            return 0

        count = 0
        now = time.time()

        for path in self.cache_dir.glob("*.json"):
            try:
                async with aiofiles.open(path, "r", encoding="utf-8") as f:
                    content = await f.read()
                data = json.loads(content)

                if now > data.get("expires_at", 0):
                    await aiofiles.os.remove(path)
                    count += 1
            except Exception:
                # Remove corrupted files
                try:
                    await aiofiles.os.remove(path)
                    count += 1
                except Exception:
                    pass

        return count

    async def get_stats(self) -> dict:
        """Get disk cache statistics."""
        if not self.cache_dir.exists():
            return {
                "size": 0,
                "total_bytes": 0,
                "cache_dir": str(self.cache_dir),
                "entries": [],
            }

        entries = []
        total_bytes = 0

        for path in self.cache_dir.glob("*.json"):
            try:
                stat = path.stat()
                total_bytes += stat.st_size

                async with aiofiles.open(path, "r", encoding="utf-8") as f:
                    content = await f.read()
                data = json.loads(content)

                entries.append(
                    {
                        "key": data.get("key", "unknown"),
                        "size_bytes": stat.st_size,
                        "expires_at": datetime.fromtimestamp(data.get("expires_at", 0)).isoformat(),
                    }
                )
            except Exception:
                pass

        return {
            "size": len(entries),
            "total_bytes": total_bytes,
            "cache_dir": str(self.cache_dir),
            "entries": entries,
        }


class ContentCache:
    """Two-tier content cache combining memory and disk storage.

    Provides fast access via memory cache (LRU) with fallback to
    persistent disk storage for longer-term caching.

    Usage:
        cache = ContentCache()

        # Get with automatic fallback
        content = await cache.get(url)

        # Set with default TTL
        await cache.set(url, html_content)
    """

    def __init__(
        self,
        memory_max_items: Optional[int] = None,
        disk_cache_dir: Optional[Path] = None,
        default_ttl_hours: Optional[int] = None,
    ):
        """Initialize two-tier cache.

        Args:
            memory_max_items: Max items in memory cache
            disk_cache_dir: Directory for disk cache
            default_ttl_hours: Default TTL in hours
        """
        settings = get_settings()
        self.default_ttl_seconds = (default_ttl_hours or settings.cache_ttl_hours) * 3600

        self._memory = LRUCache(memory_max_items or settings.cache_max_memory_items)
        self._disk = DiskCache(disk_cache_dir)

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache, checking memory first then disk.

        If found in disk but not memory, promotes to memory cache.
        """
        # Check memory first
        value = await self._memory.get(key)
        if value is not None:
            return value

        # Check disk
        value = await self._disk.get(key)
        if value is not None:
            # Promote to memory cache
            await self._memory.set(key, value, self.default_ttl_seconds)
            return value

        return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: Optional[float] = None,
        memory_only: bool = False,
    ) -> None:
        """Set value in cache.

        Args:
            key: Cache key (typically URL)
            value: Value to cache (must be JSON-serializable for disk)
            ttl_seconds: TTL in seconds (uses default if not provided)
            memory_only: If True, only store in memory cache
        """
        ttl = ttl_seconds or self.default_ttl_seconds

        # Always store in memory
        await self._memory.set(key, value, ttl)

        # Optionally store on disk
        if not memory_only:
            await self._disk.set(key, value, ttl)

    async def delete(self, key: str) -> bool:
        """Delete from both caches."""
        memory_deleted = await self._memory.delete(key)
        disk_deleted = await self._disk.delete(key)
        return memory_deleted or disk_deleted

    async def clear(self) -> dict:
        """Clear both caches. Returns counts."""
        memory_count = await self._memory.clear()
        disk_count = await self._disk.clear()
        return {"memory": memory_count, "disk": disk_count}

    async def clear_expired(self) -> dict:
        """Remove expired entries from both caches."""
        memory_count = await self._memory.clear_expired()
        disk_count = await self._disk.clear_expired()
        return {"memory": memory_count, "disk": disk_count}

    async def get_stats(self) -> dict:
        """Get statistics for both cache tiers."""
        return {
            "memory": self._memory.get_stats(),
            "disk": await self._disk.get_stats(),
        }


# Global cache instance
_content_cache: Optional[ContentCache] = None


def get_content_cache() -> ContentCache:
    """Get the global content cache instance."""
    global _content_cache
    if _content_cache is None:
        _content_cache = ContentCache()
    return _content_cache


def reset_content_cache() -> None:
    """Reset the global content cache (useful for testing)."""
    global _content_cache
    _content_cache = None
