"""Input validation for user-provided paths, URLs and settings."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse


class InputValidationError(ValueError):
    """Raised when user input fails validation. Message is user-facing."""


def validate_http_url(url: str, *, field: str = "URL") -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise InputValidationError(f"{field} must not be empty.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise InputValidationError(f"{field} must start with http:// or https://")
    if not parsed.netloc:
        raise InputValidationError(f"{field} has no host name.")
    if any(c in url for c in " \t\r\n"):
        raise InputValidationError(f"{field} must not contain whitespace.")
    return url


def is_local_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_cloud_url(url: str) -> str:
    """Cloud endpoints must use HTTPS unless they point to this machine."""
    url = validate_http_url(url, field="API endpoint")
    if url.startswith("http://") and not is_local_url(url):
        raise InputValidationError("Cloud API endpoints must use https:// (plain http is only allowed for localhost).")
    return url


def validate_import_file(path: str | Path, allowed_ext: tuple[str, ...], max_bytes: int) -> Path:
    p = Path(path)
    try:
        p = p.resolve(strict=True)
    except (OSError, RuntimeError):
        raise InputValidationError(f"File not found: {path}")
    if not p.is_file():
        raise InputValidationError(f"Not a regular file: {p.name}")
    if p.suffix.lower() not in allowed_ext:
        raise InputValidationError(f"Unsupported file type '{p.suffix}'. Supported: {', '.join(allowed_ext)}")
    size = p.stat().st_size
    if size == 0:
        raise InputValidationError(f"File is empty: {p.name}")
    if size > max_bytes:
        raise InputValidationError(f"File is too large ({size / 1e6:.1f} MB). Limit is {max_bytes / 1e6:.0f} MB.")
    return p


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str, default: str = "export") -> str:
    """Make a string safe for use as a Windows file name."""
    name = _SAFE_NAME.sub("_", name).strip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if not name or name.upper().split(".")[0] in reserved:
        name = default
    return name[:120]


def clamp_int(value: object, lo: int, hi: int, field: str) -> int:
    try:
        iv = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise InputValidationError(f"{field} must be a whole number.")
    if not lo <= iv <= hi:
        raise InputValidationError(f"{field} must be between {lo} and {hi}.")
    return iv
