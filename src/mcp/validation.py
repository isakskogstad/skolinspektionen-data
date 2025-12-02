"""Input validation utilities for MCP tool handlers.

Provides type-safe validation and sanitization of user inputs
to prevent injection attacks and ensure data integrity.
"""

from typing import Any, Optional
from urllib.parse import urlparse

# Allowed domains for URL inputs
ALLOWED_URL_DOMAINS = frozenset([
    "skolinspektionen.se",
    "www.skolinspektionen.se",
])

# Maximum string lengths
MAX_QUERY_LENGTH = 1000
MAX_URL_LENGTH = 2000

# Valid year range for school data
MIN_YEAR = 1990
MAX_YEAR = 2030

# Valid limit range
MIN_LIMIT = 1
MAX_LIMIT = 100
DEFAULT_LIMIT = 20


def validate_string(
    value: Any,
    max_length: int = MAX_QUERY_LENGTH,
    default: str = "",
    field_name: str = "value",
) -> str:
    """Validate and sanitize a string input.

    Args:
        value: Input value to validate
        max_length: Maximum allowed length
        default: Default value if input is invalid
        field_name: Name of field for error messages

    Returns:
        Validated string, truncated if necessary
    """
    if value is None:
        return default

    try:
        result = str(value)
    except (TypeError, ValueError):
        return default

    # Truncate if too long
    if len(result) > max_length:
        result = result[:max_length]

    # Remove null bytes
    result = result.replace("\x00", "")

    return result


def validate_int(
    value: Any,
    min_value: int = MIN_LIMIT,
    max_value: int = MAX_LIMIT,
    default: int = DEFAULT_LIMIT,
    field_name: str = "value",
) -> int:
    """Validate an integer input.

    Args:
        value: Input value to validate
        min_value: Minimum allowed value
        max_value: Maximum allowed value
        default: Default value if input is invalid
        field_name: Name of field for error messages

    Returns:
        Validated integer within bounds
    """
    if value is None:
        return default

    try:
        result = int(value)
    except (TypeError, ValueError):
        return default

    # Clamp to valid range
    return max(min_value, min(result, max_value))


def validate_limit(value: Any, default: int = DEFAULT_LIMIT) -> int:
    """Validate a limit/pagination parameter.

    Args:
        value: Input value
        default: Default limit

    Returns:
        Valid limit between 1 and 100
    """
    return validate_int(value, MIN_LIMIT, MAX_LIMIT, default, "limit")


def validate_year(value: Any) -> Optional[int]:
    """Validate a year parameter.

    Args:
        value: Input value

    Returns:
        Valid year or None if invalid
    """
    if value is None:
        return None

    try:
        year = int(value)
    except (TypeError, ValueError):
        return None

    if MIN_YEAR <= year <= MAX_YEAR:
        return year

    return None


def validate_url(
    value: Any,
    require_allowed_domain: bool = True,
    max_length: int = MAX_URL_LENGTH,
) -> Optional[str]:
    """Validate a URL input.

    Args:
        value: Input URL to validate
        require_allowed_domain: If True, URL must be from allowed domains
        max_length: Maximum URL length

    Returns:
        Validated URL or None if invalid
    """
    if value is None:
        return None

    try:
        url = str(value)
    except (TypeError, ValueError):
        return None

    # Check length
    if len(url) > max_length:
        return None

    # Remove null bytes
    url = url.replace("\x00", "")

    # Parse URL
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    # Validate scheme
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return None

    # If absolute URL, validate domain
    if parsed.netloc:
        hostname = parsed.hostname or ""

        # Block private IPs
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return None

        # Block private IP ranges (RFC 1918 + link-local)
        if hostname.startswith("169.254"):  # Link-local
            return None
        if hostname.startswith("10."):  # 10.0.0.0/8
            return None
        if hostname.startswith("192.168."):  # 192.168.0.0/16
            return None
        # 172.16.0.0 - 172.31.255.255 (172.16/12)
        if hostname.startswith("172."):
            try:
                second_octet = int(hostname.split(".")[1])
                if 16 <= second_octet <= 31:
                    return None
            except (IndexError, ValueError):
                pass

        # Check allowed domains
        if require_allowed_domain:
            if not any(hostname == d or hostname.endswith("." + d) for d in ALLOWED_URL_DOMAINS):
                return None

    return url


def validate_enum(
    value: Any,
    allowed_values: set,
    default: Optional[str] = None,
    field_name: str = "value",
) -> Optional[str]:
    """Validate an enum-like string input.

    Args:
        value: Input value
        allowed_values: Set of allowed values
        default: Default value if not in allowed set
        field_name: Name of field for error messages

    Returns:
        Validated value or default
    """
    if value is None:
        return default

    try:
        str_value = str(value)
    except (TypeError, ValueError):
        return default

    if str_value in allowed_values:
        return str_value

    return default


def validate_list(
    value: Any,
    max_items: int = 100,
    item_validator: callable = None,
) -> list:
    """Validate a list input.

    Args:
        value: Input value
        max_items: Maximum number of items
        item_validator: Optional function to validate each item

    Returns:
        Validated list
    """
    if value is None:
        return []

    if not isinstance(value, (list, tuple)):
        return []

    result = list(value)[:max_items]

    if item_validator:
        result = [item_validator(item) for item in result if item_validator(item) is not None]

    return result


def validate_bool(value: Any, default: bool = False) -> bool:
    """Validate a boolean input.

    Args:
        value: Input value
        default: Default value

    Returns:
        Boolean value
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")

    try:
        return bool(value)
    except (TypeError, ValueError):
        return default
