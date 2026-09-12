"""Shared HTTP client for every artwork source.

Downloads are limited to public http(s) hosts: the app can be pointed at
arbitrary URLs from the UI, so loopback/private ranges are refused.
"""

import ipaddress
import socket
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
MAX_BYTES = 40 * 1024 * 1024
IMAGE_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/x-icon",
               "image/vnd.microsoft.icon", "image/avif", "image/gif")


class DownloadError(Exception):
    pass


_session = None


def session():
    global _session
    if _session is None:
        _session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.4, status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "HEAD"])
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=24)
        _session.mount("https://", adapter)
        _session.mount("http://", adapter)
        _session.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
    return _session


def is_public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def get(url, **kwargs):
    kwargs.setdefault("timeout", 20)
    return session().get(url, **kwargs)


def download_image(url, max_bytes=MAX_BYTES):
    """Fetch an image, refusing non-public hosts and oversized payloads."""
    if not is_public_url(url):
        raise DownloadError("refusing to download from a non-public address")
    try:
        response = session().get(url, timeout=30, stream=True)
    except requests.RequestException as exc:
        raise DownloadError(f"download failed: {exc}") from exc
    if response.status_code >= 400:
        raise DownloadError(f"download failed: HTTP {response.status_code}")
    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type and content_type not in IMAGE_TYPES:
        raise DownloadError(f"not an image ({content_type or 'unknown type'})")
    chunks, size = [], 0
    for chunk in response.iter_content(65536):
        size += len(chunk)
        if size > max_bytes:
            raise DownloadError("image is too large")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise DownloadError("empty response")
    return data, content_type
