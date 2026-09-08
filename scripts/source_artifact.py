"""Bounded, hash-checked HTTPS artifact evidence with server modification age.

This proves the declared bytes and the origin's artifact modification timestamp.
It does not turn Last-Modified into a claim about the version's release date.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPSConnection
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse, urlunparse
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

import registry

MAX_BYTES = 1024 * 1024 * 1024
MAX_DECLARED_BYTES = 16 * MAX_BYTES


def public_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value)
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or getattr(address, "is_site_local", False)
        or "%" in value
    ):
        raise ValueError("Artifact evidence requires a public network address")
    # Transition addresses must not conceal a private IPv4 destination.
    embedded = (
        [address.sixtofour]
        if isinstance(address, ipaddress.IPv6Address) and address.sixtofour
        else []
    )
    if isinstance(address, ipaddress.IPv6Address) and address.teredo:
        embedded.extend(address.teredo)
    for child in embedded:
        public_address(str(child))
    return address


def public_connection(
    address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None
):
    """Resolve once, validate the entire answer, then dial only those numeric IPs."""
    host, port = address
    if port != 443 or source_address is not None:
        raise ValueError("Artifact evidence requires direct public HTTPS")
    resolved = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    endpoints = []
    for family, kind, protocol, _, target in resolved:
        ip = public_address(target[0])
        if (
            family not in (socket.AF_INET, socket.AF_INET6)
            or kind != socket.SOCK_STREAM
            or protocol != socket.IPPROTO_TCP
            or target[1] != 443
            or (family == socket.AF_INET) != (ip.version == 4)
            or (family == socket.AF_INET6 and target[3] != 0)
        ):
            raise ValueError("Artifact DNS lacks a public HTTPS endpoint")
        # Reconstruct a canonical numeric sockaddr; connect performs no new lookup.
        target = (str(ip), 443) if ip.version == 4 else (str(ip), 443, 0, 0)
        endpoints.append((family, kind, protocol, target))
    if not endpoints:
        raise ValueError("Artifact DNS lacks a public HTTPS endpoint")
    for family, kind, protocol, target in endpoints:
        connected = socket.socket(family, kind, protocol)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connected.settimeout(timeout)
            connected.connect(target)
            return connected
        except OSError:
            connected.close()
    raise OSError("Public artifact origin is unavailable")


class PublicHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection

    def connect(self):
        if self._tunnel_host:
            raise ValueError("Artifact evidence does not permit proxy tunnels")
        # Keep the standard verified TLS handshake and original hostname/SNI.
        super().connect()


class PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(PublicHTTPSConnection, request, context=self._context)


def artifact_url(value: str, *, signed_github_cdn: bool = False) -> str:
    registry.artifact_url(value)
    parsed = urlparse(value)
    host = parsed.hostname.lower()
    if (
        (
            parsed.query
            and not (
                signed_github_cdn and host == "release-assets.githubusercontent.com"
            )
        )
        or parsed.port not in (None, 443)
        or re.search(r"[\x00-\x20\x7f]", value)
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or "." not in host
        or any(
            part.lower() in {"latest", "stable", "current", "nightly", "snapshot"}
            for part in unquote(parsed.path).split("/")
        )
    ):
        raise ValueError(
            "Artifact evidence requires an explicit public HTTPS object URL"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        public_address(str(address))
    return value


class PublicRedirects(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 1

    def __init__(self, source: str = ""):
        super().__init__()
        parsed = urlparse(source)
        self.github_release = parsed.hostname == "github.com" and bool(
            re.fullmatch(
                r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[^/]+/[^/]+",
                parsed.path,
            )
        )

    def redirect_request(self, request, fp, code, message, headers, newurl):
        artifact_url(newurl, signed_github_cdn=self.github_release)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def inspect(
    url: str, digest: str, now: datetime, *, max_bytes: int | None = None
) -> dict:
    """Read exact public bytes; missing or future origin metadata is an error."""
    artifact_url(url)
    registry.digest(digest)
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_DECLARED_BYTES:
        raise ValueError(
            "Artifact download limit must be a positive integer at most 16 GiB"
        )
    if now.tzinfo is None:
        raise ValueError("Artifact observation requires a timezone")
    request = Request(
        url,
        headers={"User-Agent": "chainman", "Accept-Encoding": "identity"},
    )
    try:
        redirects = PublicRedirects(url)
        with build_opener(ProxyHandler({}), PublicHTTPSHandler(), redirects).open(
            request, timeout=60
        ) as response:
            final_url = artifact_url(
                response.geturl(), signed_github_cdn=redirects.github_release
            )
            if response.status != 200:
                raise ValueError("Artifact origin did not return the complete object")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise ValueError(
                    "Artifact origin returned a transformed representation"
                )
            value = response.headers.get("Last-Modified")
            if not value:
                raise ValueError("Artifact origin lacks dated modification evidence")
            try:
                modified = parsedate_to_datetime(value)
            except (ValueError, TypeError, OverflowError):
                raise ValueError(
                    "Artifact origin modification date is malformed"
                ) from None
            if modified.tzinfo is None or modified > now:
                raise ValueError(
                    "Artifact origin date lacks a timezone or is in the future"
                )
            length = response.headers.get("Content-Length")
            if length and (not length.isdecimal() or int(length) > max_bytes):
                raise ValueError("Artifact exceeds the bounded download size")
            hashed, size = hashlib.sha256(), 0
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > max_bytes:
                    raise ValueError("Artifact exceeds the bounded download size")
                hashed.update(block)
            if length and size != int(length):
                raise ValueError("Artifact download was incomplete")
    except (HTTPError, URLError, TimeoutError):
        raise ValueError("Public artifact origin is unavailable") from None
    if "sha256:" + hashed.hexdigest() != digest:
        raise ValueError("Artifact bytes disagree with the declared immutable SHA256")
    return {
        "url": url,
        # Signed CDN query strings are transport credentials, not durable identity.
        "resolved_url": urlunparse(urlparse(final_url)._replace(query="")),
        "digest": digest,
        "size": size,
        "published": modified.astimezone(timezone.utc).isoformat(),
        "age_basis": "origin-artifact-last-modified",
    }


def audit(
    url: str, digest: str, policy: dict, now: datetime, *, max_bytes: int | None = None
) -> dict:
    result = inspect(url, digest, now, max_bytes=max_bytes)
    if registry.timestamp(result["published"]) > now - timedelta(
        days=registry.minimum_age(policy)
    ):
        raise ValueError("Artifact bytes lack the required origin modification age")
    return result
