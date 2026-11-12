"""Version selection from dated registry artifacts, with explicit age evidence."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from contextlib import contextmanager
import base64
import binascii
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from email.message import Message
from functools import lru_cache
import json
import hashlib
import os
from http.client import HTTPException
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Lock
from typing import IO, Literal, NoReturn, Protocol, Self, overload
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import xml.etree.ElementTree as ET

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version as PythonVersion
from packaging.utils import canonicalize_name
from semantic_version import NpmSpec, Version as Semver
import adapter_data
from adapter_data import array, table, text
import maven_version
from maven_version import MavenVersion

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_RETRY_WAIT_SECONDS = 60
_crates_request_lock = Lock()
_crates_last_request: float | None = None
_github_context_unset = object()
_github_context: object = _github_context_unset
_github_context_lock = Lock()
# A rejected credential (401 or an SSO-restricted token) leaves the rest of the
# command anonymous instead of failing it.
_github_rejected = False
GITHUB_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/-]+=*")
CREDENTIAL_TIMEOUT_SECONDS = 5
IMMUTABLE_CACHE_SCHEMA = 1


class GitHubSettings:
    """Command-level GitHub behavior; library imports and tests keep the defaults."""

    def __init__(self) -> None:
        self.discover = False
        self.git_refs = False
        self.cache: Path | None = None


_github_settings = GitHubSettings()


@contextmanager
def github_session(*, cache_home: Path | None = None) -> Iterator[None]:
    """Enable credential discovery, Git tag refs and the immutable answer cache.

    Update and query entry points wrap their work in this session; the previous
    settings are restored afterwards, so in-process callers and tests do not
    inherit durable caching or credential discovery.
    """
    base = cache_home or Path(
        os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    )
    previous = (
        _github_settings.discover,
        _github_settings.git_refs,
        _github_settings.cache,
    )
    _github_settings.discover = True
    _github_settings.git_refs = True
    _github_settings.cache = (
        base / "chainman" / "github-immutable" / f"v{IMMUTABLE_CACHE_SCHEMA}"
    )
    try:
        yield
    finally:
        (
            _github_settings.discover,
            _github_settings.git_refs,
            _github_settings.cache,
        ) = previous


@contextmanager
def request_window(host: str) -> Iterator[None]:
    """Leave one second after each crates.io response before the next request."""
    global _crates_last_request
    if host != "crates.io":
        yield
        return
    # Timestamp completion under the lock. A thread delayed between timestamp
    # reservation and actual dispatch must not leave a stale gate for its peer.
    # Cache hits never enter; sparse-index and CDN hosts are separate services.
    with _crates_request_lock:
        if _crates_last_request is not None:
            remaining = _crates_last_request + 1 - time.monotonic()
            while remaining > 0:
                time.sleep(remaining)
                remaining = _crates_last_request + 1 - time.monotonic()
        try:
            yield
        finally:
            _crates_last_request = time.monotonic()


def observation_time() -> datetime:
    """Host UTC when metadata becomes immutable evidence, not the update cutoff."""
    return datetime.now(timezone.utc)


def validate_publication(published: datetime) -> None:
    if not isinstance(published, datetime) or published.utcoffset() is None:
        raise ValueError("Registry publication time requires a timezone")
    if published > observation_time():
        raise ValueError("Future registry publication time")


@dataclass(frozen=True)
class Artifact:
    url: str
    digest: str
    published: datetime

    def __post_init__(self) -> None:
        validate_publication(self.published)


@dataclass(frozen=True)
class Release:
    version: str
    published: datetime
    identity: str = ""
    python: str = ""
    artifacts: tuple[Artifact, ...] = ()
    deprecated: bool = False

    def __post_init__(self) -> None:
        validate_publication(self.published)


class RegistryHTTPError(ValueError):
    def __init__(self, status: int, host: str, *, excessive_wait: bool = False):
        self.status = status
        self.package_context = ""
        suffix = (
            f"; Retry-After exceeds {MAX_RETRY_WAIT_SECONDS}-second wait limit"
            if excessive_wait
            else ""
        )
        super().__init__(f"Registry HTTP {status} from {host}{suffix}")

    def __str__(self) -> str:
        message = super().__str__()
        return (
            f"{message} ({self.package_context})" if self.package_context else message
        )


def retry_delay(value: object, attempt: int, status: int, host: str) -> float:
    """Bound server-requested waits without retrying earlier than a valid hint."""
    delay = float(2**attempt)
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"[0-9]+", value):
            digits = value.lstrip("0") or "0"
            seconds = int(digits) if len(digits) <= 2 else MAX_RETRY_WAIT_SECONDS + 1
            delay = max(delay, seconds)
        elif len(value) <= 128:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None and re.fullmatch(
                    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} "
                    r"(?: [0-9]|[0-9]{2}) [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}",
                    value,
                ):
                    date = date.replace(tzinfo=timezone.utc)
                if date.utcoffset() is not None:
                    delay = max(delay, (date - observation_time()).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    if delay > MAX_RETRY_WAIT_SECONDS:
        raise RegistryHTTPError(status, host, excessive_wait=True) from None
    return delay


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Missing registry publication age")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Registry timestamp lacks a timezone")
    return result.astimezone(timezone.utc)


def artifact_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Missing artifact URL")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(
            "Artifact URL must be credential-free HTTPS without a fragment"
        )
    return value


def digest(value: str, *, npm: bool = False) -> str:
    """Canonicalize supported immutable hash evidence; never accept a missing hash."""
    if not isinstance(value, str):
        raise ValueError("Missing artifact digest")
    if npm:
        match = re.fullmatch(
            r"(sha1|sha256|sha384|sha512)-([A-Za-z0-9+/]+={0,2})", value
        )
        if match:
            algorithm, encoded = match.groups()
            try:
                raw = base64.b64decode(encoded, validate=True)
            except binascii.Error:
                raw = b""
            if (
                len(raw)
                == {"sha1": 20, "sha256": 32, "sha384": 48, "sha512": 64}[algorithm]
            ):
                return f"{algorithm}:{raw.hex()}"
    elif re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        return value
    raise ValueError("Unsupported or malformed artifact digest")


def package_name(provider: str, name: str) -> str:
    return canonicalize_name(name) if provider == "pypi" else name


def validate_constraint(provider: str, value: object) -> str | tuple[str, ...]:
    if isinstance(value, str):
        return value
    if (
        provider != "npm"
        or not isinstance(value, (list, tuple))
        or not 1 <= len(value) <= 128
        or any(not isinstance(bound, str) or not bound.strip() for bound in value)
        or sum(len(bound) for bound in value) > 65536
        or sum(len(bound.split("||")) for bound in value) > 128
    ):
        raise ValueError(
            "npm constraint conjunction requires bounded nonempty range strings"
        )
    # Validate every member before any membership test can short-circuit.
    for bound in value:
        NpmSpec(bound)
    return tuple(value)


def constraint(
    provider: str, policy: Mapping[str, object], name: str
) -> str | tuple[str, ...]:
    rules = [
        rule
        for key, rule in adapter_data.table(
            policy.get("constraints", {}), "Compatibility constraints"
        ).items()
        if key.partition(":")[0] == provider
        and package_name(provider, key.partition(":")[2])
        == package_name(provider, name)
    ]
    if len(rules) > 1:
        raise ValueError("Duplicate normalized compatibility constraints")
    rule = adapter_data.table(rules[0], "Compatibility constraint") if rules else {}
    if rule and not str(rule.get("reason", "")).strip():
        raise ValueError("Compatibility constraints require a reason")
    return validate_constraint(provider, rule.get("range", ""))


def valid_github_token(token: str) -> bool:
    return len(token) <= 4096 and GITHUB_TOKEN_PATTERN.fullmatch(token) is not None


def credential_environment() -> dict[str, str]:
    """Strictly non-interactive helper environment without repository routing."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "GIT_TRACE",
            "GIT_TRACE_CURL",
            "GIT_CURL_VERBOSE",
            "GH_DEBUG",
            "DEBUG",
        }
    }
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="never",
        GH_PROMPT_DISABLED="1",
        GH_NO_UPDATE_NOTIFIER="1",
    )
    return env


def _credential_output(argv: list[str], data: bytes | None) -> bytes:
    try:
        result = subprocess.run(
            argv,
            input=data if data is not None else b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=credential_environment(),
            cwd="/",
            timeout=CREDENTIAL_TIMEOUT_SECONDS,
            check=False,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return b""
    return result.stdout if result.returncode == 0 else b""


def discover_github_token() -> str:
    """Read-only, non-interactive lookup: gh first, then Git's credential helpers.

    Any failure, timeout or malformed value is anonymous. The value is never logged,
    cached or persisted, and Git credential approve/reject is never invoked.
    """
    if os.environ.get("CHAINMAN_GITHUB_CREDENTIALS", "") == "off":
        return ""
    token = (
        _credential_output(["gh", "auth", "token", "--hostname", "github.com"], None)
        .decode("ascii", errors="replace")
        .strip()
    )
    if not token:
        filled = _credential_output(
            [
                "git",
                "-c",
                "core.askPass=",
                "-c",
                "credential.interactive=never",
                "credential",
                "fill",
            ],
            b"protocol=https\nhost=github.com\n\n",
        )
        for line in filled.decode("utf-8", errors="replace").splitlines():
            if line.startswith("password="):
                token = line.removeprefix("password=")
                break
    return token if token and valid_github_token(token) else ""


def github_token() -> str:
    """One credential context per command, including cached/fresh reads.

    Precedence: GITHUB_TOKEN, GH_TOKEN, then (when the command enabled discovery)
    `gh auth token` and `git credential fill` for https://github.com.
    """
    global _github_context
    explicit = (os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GH_TOKEN", ""))
    for value, name in zip(explicit, ("GITHUB_TOKEN", "GH_TOKEN"), strict=True):
        if value and not valid_github_token(value):
            raise ValueError(f"Invalid {name} header value") from None
    with _github_context_lock:
        if _github_context is _github_context_unset:
            token = explicit[0] or explicit[1]
            if not token and _github_settings.discover:
                token = discover_github_token()
            _github_context = (explicit, token)
        assert isinstance(_github_context, tuple)
        if explicit != _github_context[0]:
            name = (
                "GITHUB_TOKEN" if explicit[0] != _github_context[0][0] else "GH_TOKEN"
            )
            raise ValueError(
                f"{name} changed during this command; start a new command"
            ) from None
        return "" if _github_rejected else str(_github_context[1])


def reject_github_token(status: int) -> None:
    global _github_rejected
    with _github_context_lock:
        if not _github_rejected:
            _github_rejected = True
            print(
                f"GitHub rejected the configured credential (HTTP {status}); "
                "continuing anonymously for this command.",
                file=sys.stderr,
            )


class GitHubRateLimitError(RegistryHTTPError):
    """Exhausted GitHub API quota, with its reset time and credential guidance."""

    def __init__(self, status: int, reset: str | None, authenticated: bool):
        super().__init__(status, "api.github.com")
        when = "an unknown time"
        if reset is not None and reset.isdigit():
            when = datetime.fromtimestamp(int(reset), timezone.utc).isoformat()
        guidance = (
            "the authenticated quota resets then"
            if authenticated
            else "anonymous requests share a small per-address quota; provide a token "
            "with GITHUB_TOKEN or GH_TOKEN, `gh auth login`, or a Git credential helper "
            "for https://github.com (unless CHAINMAN_GITHUB_CREDENTIALS=off)"
        )
        self.reset = when
        self.args = (f"GitHub API rate limit exhausted until {when}; {guidance}",)


def rate_limited(status: int, headers: Message | None) -> bool:
    return (
        status in (403, 429)
        and headers is not None
        and headers.get("X-RateLimit-Remaining") == "0"
    )


def sso_restricted(status: int, headers: Message | None) -> bool:
    return status == 403 and headers is not None and bool(headers.get("X-GitHub-SSO"))


class GitHubNoRedirect(HTTPRedirectHandler):
    """Reject before urllib parses Location or constructs a successor request."""

    def http_error_302(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
    ) -> NoReturn:
        try:
            fp.close()
        finally:
            raise RegistryHTTPError(code, "api.github.com") from None

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _fetch(
    url: str,
    accept: str = "application/json",
    method: str = "GET",
    *,
    fresh: bool = False,
    anonymous: bool = False,
) -> tuple[bytes, dict[str, str]]:
    token = github_token()
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not parsed.hostname
    ):
        raise ValueError("Registry requests require credential-free HTTPS URLs")
    try:
        authenticated = (
            not anonymous
            and bool(token)
            and (
                parsed.hostname == "api.github.com"
                and parsed.username is None
                and parsed.password is None
                and parsed.port in (None, 443)
            )
        )
    except ValueError:
        raise ValueError("Registry URL has an invalid port") from None
    for attempt in range(3):
        delay = float(2**attempt)
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "chainman (https://github.com/chainmandev/chainman)",
                    "Accept": accept,
                    **({"Cache-Control": "no-cache"} if fresh else {}),
                },
                method=method,
            )
            if authenticated:
                request.add_unredirected_header("Authorization", "Bearer " + token)
            open_request = (
                build_opener(GitHubNoRedirect()).open if authenticated else urlopen
            )
            with (
                request_window(parsed.hostname),
                open_request(request, timeout=30) as response,
            ):
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError(
                        f"Registry response exceeds 64 MiB from {parsed.hostname}"
                    )
                return body, dict(response.headers.items())
        except HTTPError as exc:
            fallback = False
            try:
                if parsed.hostname == "api.github.com" and rate_limited(
                    exc.code, exc.headers
                ):
                    raise GitHubRateLimitError(
                        exc.code,
                        exc.headers.get("X-RateLimit-Reset") if exc.headers else None,
                        authenticated,
                    ) from None
                if authenticated and (
                    exc.code == 401 or sso_restricted(exc.code, exc.headers)
                ):
                    fallback = True
                elif exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise RegistryHTTPError(exc.code, parsed.hostname) from None
                else:
                    delay = retry_delay(
                        exc.headers.get("Retry-After") if exc.headers else None,
                        attempt,
                        exc.code,
                        parsed.hostname,
                    )
            finally:
                exc.close()
            if fallback:
                # An unusable credential is dropped for the rest of the command.
                reject_github_token(exc.code)
                return _fetch(url, accept, method, fresh=fresh, anonymous=True)
            time.sleep(delay)
            continue
        except (URLError, TimeoutError):
            if attempt == 2:
                raise ValueError(f"Registry unavailable: {parsed.hostname}") from None
        except RegistryHTTPError:
            raise
        except (OSError, HTTPException):
            if not authenticated:
                raise
            if attempt == 2:
                raise ValueError("Registry unavailable: api.github.com") from None
        except (ValueError, UnicodeError):
            if not authenticated:
                raise
            raise ValueError("Registry request failed: api.github.com") from None
        time.sleep(delay)
    raise AssertionError("unreachable")


@lru_cache(maxsize=2048)
def _cached_fetch(
    url: str,
    accept: str = "application/json",
    method: str = "GET",
    anonymous: bool = False,
) -> tuple[bytes, dict[str, str]]:
    if anonymous:
        return _fetch(url, accept, method, anonymous=True)
    return _fetch(url, accept, method)


class RegistryFetch:
    def __call__(
        self,
        url: str,
        accept: str = "application/json",
        method: str = "GET",
        *,
        anonymous: bool = False,
    ) -> tuple[bytes, dict[str, str]]:
        github_token()  # Check before a cache hit can return the command's evidence.
        return _cached_fetch(url, accept, method, anonymous)

    # Clearing metadata never resets the command's credential context.
    cache_clear = staticmethod(_cached_fetch.cache_clear)
    cache_info = staticmethod(_cached_fetch.cache_info)


fetch = RegistryFetch()


def data(url: str) -> object:
    return json.loads(fetch(url)[0])


def _fresh_data(url: str) -> object:
    return json.loads(_fetch(url, fresh=True)[0])


def _immutable_path(key: list[str]) -> Path | None:
    root = _github_settings.cache
    if root is None:
        return None
    digest = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()
    return root / digest[:2] / (digest + ".json")


def immutable_get(key: list[str]) -> object | None:
    """Return a stored answer only when the file restates its complete key.

    Unreadable, foreign or malformed entries are ignored and replaced on demand.
    """
    path = _immutable_path([str(IMMUTABLE_CACHE_SCHEMA), *key])
    if path is None:
        return None
    try:
        if path.is_symlink() or not path.is_file():
            return None
        entry = json.loads(path.read_bytes()[: 1024 * 1024])
    except (OSError, ValueError):
        return None
    if (
        not isinstance(entry, dict)
        or entry.get("key") != [str(IMMUTABLE_CACHE_SCHEMA), *key]
        or "value" not in entry
    ):
        return None
    value: object = entry["value"]
    return value


def immutable_put(key: list[str], value: object) -> None:
    """Best-effort atomic write of an answer that cannot change for its key."""
    full = [str(IMMUTABLE_CACHE_SCHEMA), *key]
    path = _immutable_path(full)
    if path is None:
        return
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink():
            return
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".entry-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"key": full, "value": value}, stream, sort_keys=True)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError:
        return


def github_commit_date(repository: str, commit: str) -> datetime:
    """Committer time of an exact commit; content-addressed, so cached durably."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not (
        re.fullmatch(r"[a-f0-9]{40}", commit)
    ):
        raise ValueError("Expected a GitHub repository and exact commit identity")
    key = ["commit-committer-date", repository, commit]
    cached = immutable_get(key)
    if isinstance(cached, str):
        try:
            return timestamp(cached)
        except ValueError:
            pass
    item = table(
        data(f"https://api.github.com/repos/{repository}/commits/{commit}"),
        "GitHub commit",
    )
    if item.get("sha") != commit:
        raise ValueError("GitHub returned a different commit identity")
    details = table(item["commit"], "GitHub commit details")
    committer = table(details["committer"], "GitHub committer")
    published = timestamp(committer["date"])
    immutable_put(key, published.isoformat())
    return published


def git_tag_target(repository: str, tag: str) -> str | None:
    """Resolve one tag through the Git protocol, peeling annotated tags.

    Returns None when the Git transport cannot answer (unavailable Git, network,
    private repository or a missing ref); callers then use the REST API.
    """
    if not _github_settings.git_refs or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}", tag
    ):
        return None
    try:
        import git_runtime

        with git_runtime.isolated_repository() as selected:
            listing = git_runtime.git(
                selected,
                "-c",
                "protocol.version=2",
                "-c",
                "credential.helper=",
                "ls-remote",
                f"https://github.com/{repository}.git",
                f"refs/tags/{tag}",
            ).decode("utf-8")
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return None
    refs: dict[str, str] = {}
    for line in listing.splitlines():
        value, _, name = line.partition("\t")
        if not re.fullmatch(r"[a-f0-9]{40}", value):
            return None
        if name in (f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"):
            if name in refs:
                return None
            refs[name] = value
    return refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))


def github_quota_check() -> None:
    """Fail early when the core API quota is exhausted; /rate_limit is not counted.

    Transport failures are ignored here: the first real request reports them.
    """
    try:
        body, _ = _fetch("https://api.github.com/rate_limit")
        core = table(table(json.loads(body), "GitHub rate limit")["resources"], "Rate")[
            "core"
        ]
        resources = table(core, "Core rate limit")
    except GitHubRateLimitError:
        raise
    except (ValueError, KeyError, OSError):
        return
    remaining, reset = resources.get("remaining"), resources.get("reset")
    if type(remaining) is int and remaining <= 0:
        raise GitHubRateLimitError(
            403, str(reset) if type(reset) is int else None, bool(github_token())
        )


class VersionRank(Protocol):
    """Library values ordered within one provider; providers are never mixed."""

    def __lt__(self, other: Self, /) -> bool: ...
    def __le__(self, other: Self, /) -> bool: ...
    def __gt__(self, other: Self, /) -> bool: ...
    def __ge__(self, other: Self, /) -> bool: ...


type SemanticProvider = Literal[
    "npm", "go", "github", "docker", "crates", "pub", "swift"
]
type PythonProvider = Literal["pypi"]


@overload
def version(provider: SemanticProvider, text: str) -> Semver | None: ...
@overload
def version(provider: PythonProvider, text: str) -> PythonVersion | None: ...
@overload
def version(provider: Literal["maven"], text: str) -> MavenVersion | None: ...
@overload
def version(provider: str, text: str) -> VersionRank | None: ...
def version(provider: str, text: str) -> VersionRank | None:
    try:
        if provider == "maven":
            maven = MavenVersion(text)
            return maven if maven.stable else None
        if provider == "pypi":
            result = PythonVersion(text)
            if result.is_prerelease or result.is_devrelease:
                return None
            return result
        # Deliberately do not coerce prerelease suffixes or incomplete tags.
        semantic = Semver(text.removeprefix("v"))
        return None if semantic.prerelease else semantic
    except (ValueError, InvalidVersion):
        return None


def stable_version(provider: str, value: str) -> VersionRank:
    """Rank a candidate after selection has required a stable release."""
    result = version(provider, value)
    if result is None:
        raise ValueError(f"Expected a stable {provider} version: {value}")
    return result


def compatible(
    provider: str, value: str, constraint: str | tuple[str, ...] | list[str]
) -> bool:
    constraint = validate_constraint(provider, constraint)
    if isinstance(constraint, tuple):
        candidate = Semver(value.removeprefix("v"))
        return all(candidate in NpmSpec(bound) for bound in constraint)
    if not constraint:
        return True
    if provider == "maven":
        return maven_version.compatible(value, constraint)
    if provider == "pypi":
        return PythonVersion(value) in SpecifierSet(constraint)
    return Semver(value.removeprefix("v")) in NpmSpec(constraint)


@overload
def lock_version(provider: SemanticProvider, value: str) -> Semver | None: ...
@overload
def lock_version(provider: PythonProvider, value: str) -> PythonVersion | None: ...
@overload
def lock_version(provider: Literal["maven"], value: str) -> MavenVersion | None: ...
@overload
def lock_version(provider: str, value: str) -> VersionRank | None: ...
def lock_version(provider: str, value: str) -> VersionRank | None:
    """Parse locked identities without making prereleases selectable releases."""
    if provider == "maven":
        try:
            return MavenVersion(value)
        except ValueError:
            return None
    if provider != "npm":
        return version(provider, value)
    try:
        return Semver(value)
    except (ValueError, TypeError):
        return None


def minimum_age(policy: Mapping[str, object]) -> int:
    days = policy.get("minimum_age_days", 30)
    if type(days) is not int or days < 0:
        raise ValueError("minimum_age_days must be a nonnegative integer")
    return days


def policy_exceptions(
    provider: str, policy: Mapping[str, object], name: str
) -> Iterator[adapter_data.AgeException]:
    for raw in adapter_data.array(
        policy.get("exceptions", []), "Release age exceptions"
    ):
        exception = adapter_data.table(raw, "Release age exception")
        scope, _, package = adapter_data.text(
            exception.get("package", ""), "Exception package"
        ).partition(":")
        if scope != provider or package_name(provider, package) != package_name(
            provider, name
        ):
            continue
        yield adapter_data.AgeException.decode(exception)


@overload
def minimum_safe(
    provider: SemanticProvider,
    policy: Mapping[str, object],
    name: str,
    candidate: str | None = None,
) -> Semver | None: ...
@overload
def minimum_safe(
    provider: PythonProvider,
    policy: Mapping[str, object],
    name: str,
    candidate: str | None = None,
) -> PythonVersion | None: ...
@overload
def minimum_safe(
    provider: Literal["maven"],
    policy: Mapping[str, object],
    name: str,
    candidate: str | None = None,
) -> MavenVersion | None: ...
@overload
def minimum_safe(
    provider: str, policy: Mapping[str, object], name: str, candidate: str | None = None
) -> VersionRank | None: ...
def minimum_safe(
    provider: str, policy: Mapping[str, object], name: str, candidate: str | None = None
) -> VersionRank | None:
    floors: list[VersionRank] = []
    for exception in policy_exceptions(provider, policy, name):
        floor = version(provider, exception.minimum_safe)
        admitted = version(provider, exception.version)
        timestamp(exception.expires)
        if floor is None or admitted is None or admitted < floor:
            raise ValueError("Invalid exception safe floor")
        if exception.applies_to:
            validate_constraint(provider, exception.applies_to)
            if not all(
                compatible(provider, value, exception.applies_to)
                for value in (exception.version, exception.minimum_safe)
            ):
                raise ValueError(
                    "Exception version and safe floor must belong to applies_to"
                )
            if candidate is None or not compatible(
                provider, candidate, exception.applies_to
            ):
                continue
        floors.append(floor)
    return max(floors) if floors else None


def meets_safe_floor(
    provider: str, policy: Mapping[str, object], name: str, value: str
) -> bool:
    """Evaluate every applicable floor against this exact installed/candidate version."""
    floor = minimum_safe(provider, policy, name, value)
    rank = lock_version(provider, value)
    return floor is None or (rank is not None and rank >= floor)


def latest_publications(releases: list[Release]) -> dict[str, datetime]:
    dates: dict[str, datetime] = {}
    for release in releases:
        dates[release.version] = max(
            dates.get(release.version, release.published), release.published
        )
    return dates


def maturity(
    provider: str,
    releases: list[Release],
    policy: Mapping[str, object],
    name: str,
    now: datetime,
) -> list[Release]:
    bound = constraint(provider, policy, name)
    minimum_safe(provider, policy, name)
    cutoff = now - timedelta(days=minimum_age(policy))
    candidates = []
    dates = latest_publications(releases)
    for release in releases:
        rank = version(provider, release.version)
        if (
            rank is None
            or not meets_safe_floor(provider, policy, name, release.version)
            or release.python == "unsupported"
            or release.deprecated
            or not compatible(provider, release.version, bound)
        ):
            continue
        if dates[release.version] <= cutoff:
            candidates.append(release)
    return candidates


def active_exceptions(
    provider: str,
    releases: list[Release],
    policy: Mapping[str, object],
    name: str,
    now: datetime,
    *,
    scope_versions: set[str] | None = None,
) -> list[Release]:
    mature = maturity(provider, releases, policy, name, now)
    bound = constraint(provider, policy, name)
    candidates = []
    dates = latest_publications(releases)
    # An exception admits one exact version only while a mature safe version is absent.
    for exception in policy_exceptions(provider, policy, name):
        if (
            exception.applies_to
            and scope_versions is not None
            and not any(
                compatible(provider, value, exception.applies_to)
                for value in scope_versions
            )
        ):
            continue
        expiry = timestamp(exception.expires)
        safe = version(provider, exception.minimum_safe)
        admitted = version(provider, exception.version)
        if safe is None or admitted is None or admitted < safe:
            raise ValueError("Invalid exception safe floor")
        if exception.applies_to and not any(
            version(provider, release.version) is not None
            and compatible(provider, release.version, exception.applies_to)
            and compatible(provider, release.version, bound)
            for release in releases
        ):
            continue
        if any(
            stable_version(provider, release.version) >= safe
            and compatible(provider, release.version, exception.applies_to)
            for release in mature
        ):
            continue
        if expiry <= now:
            raise ValueError(f"Expired security exception for {name}")
        for release in releases:
            if (
                release.version == exception.version
                and dates[release.version] <= now
                and meets_safe_floor(provider, policy, name, release.version)
                and release.python != "unsupported"
                and not release.deprecated
                and compatible(provider, release.version, bound)
            ):
                candidates.append(release)
    return candidates


def eligible(
    provider: str,
    releases: list[Release],
    policy: Mapping[str, object],
    name: str,
    now: datetime,
) -> list[Release]:
    candidates = maturity(provider, releases, policy, name, now) + active_exceptions(
        provider, releases, policy, name, now
    )
    if not candidates:
        raise ValueError(
            f"No eligible stable release with verified age for {provider}:{name}"
        )
    return candidates


def select(
    provider: str,
    releases: list[Release],
    policy: Mapping[str, object],
    name: str,
    now: datetime,
) -> Release:
    return max(
        eligible(provider, releases, policy, name, now),
        key=lambda item: stable_version(provider, item.version),
    )


def github_releases(repository: str) -> list[Release]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected a GitHub owner/repository")
    releases = []
    for page in range(1, 101):
        entries = data(
            f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}"
        )
        if not isinstance(entries, list):
            raise ValueError("Malformed GitHub release response")
        for raw in entries:
            item = table(raw, "GitHub release")
            if (
                not item["draft"]
                and not item["prerelease"]
                and version("github", text(item["tag_name"], "GitHub release tag"))
                is not None
            ):
                releases.append(
                    Release(
                        text(item["tag_name"], "GitHub release tag"),
                        timestamp(item["published_at"]),
                    )
                )
        if len(entries) < 100:
            return releases
    raise ValueError(
        "GitHub release pagination ceiling reached; selection is incomplete"
    )


def github_commit(repository: str, tag: str, *, fresh: bool = False) -> str:
    """Resolve a tag, optionally refreshing every hop outside the snapshot cache.

    The tag name is always read live. The Git protocol answers without REST quota;
    annotated tag objects and commit identities are content-addressed and cached.
    """
    target = git_tag_target(repository, tag)
    if target is not None:
        # A tag may name a non-commit object; the commit lookup proves its type.
        try:
            github_commit_date(repository, target)
        except RegistryHTTPError as exc:
            if exc.status not in (404, 422):
                raise
            raise ValueError(
                "Release tag does not resolve to a bounded commit identity"
            ) from None
        return target
    read = _fresh_data if fresh else data
    item = table(
        table(
            read(
                f"https://api.github.com/repos/{repository}/git/ref/tags/{quote(tag, safe='')}"
            ),
            "GitHub reference",
        )["object"],
        "GitHub object",
    )
    for _ in range(10):
        sha = text(item["sha"], "GitHub object SHA")
        if item["type"] == "commit" and re.fullmatch(r"[a-f0-9]{40}", sha):
            return sha
        if item["type"] != "tag" or not re.fullmatch(r"[a-f0-9]{40}", sha):
            break
        key = ["tag-object-target", repository, sha]
        cached = immutable_get(key)
        if (
            isinstance(cached, dict)
            and cached.get("type") in {"commit", "tag"}
            and isinstance(cached.get("sha"), str)
        ):
            item = cached
            continue
        item = table(
            table(
                read(f"https://api.github.com/repos/{repository}/git/tags/{sha}"),
                "GitHub tag",
            )["object"],
            "GitHub object",
        )
        if item.get("type") in {"commit", "tag"} and isinstance(item.get("sha"), str):
            immutable_put(key, {"type": item["type"], "sha": item["sha"]})
    raise ValueError("Release tag does not resolve to a bounded commit identity")


def swift_releases(package: str, *, exact: str | None = None) -> list[Release]:
    """Enrich all release candidates, or only the requested Swift version's tags."""
    if exact is not None and (
        not isinstance(exact, str)
        or exact.startswith("v")
        or version("swift", exact) is None
    ):
        raise ValueError("Exact Swift metadata requires a canonical stable version")
    import source_updates

    source_updates.repository_name(package)
    return [
        Release(
            item.version.removeprefix("v"),
            max(
                item.published,
                source_updates.commit_time(
                    package, github_commit(package, item.version)
                ),
            ),
            item.version,
        )
        for item in github_releases(package)
        if exact is None or item.version.removeprefix("v") == exact
    ]


def go_path(package: str) -> str:
    if (
        not isinstance(package, str)
        or not re.fullmatch(r"[A-Za-z0-9._~/-]+", package)
        or "." not in package.split("/")[0]
        or any(part in ("", ".", "..") for part in package.split("/"))
    ):
        raise ValueError("Unsupported public Go module path")
    return "".join("!" + c.lower() if c.isupper() else c for c in package)


def go_version(value: str) -> bool:
    # Audit canonical pseudoversions too; selection still excludes prereleases.
    if not isinstance(value, str) or not value.startswith("v"):
        return False
    try:
        parsed = Semver(value[1:])
        return not parsed.build or parsed.build == ("incompatible",)
    except ValueError:
        return False


def go_info(package: str, value: str) -> Release:
    if not go_version(value):
        raise ValueError("Invalid Go module version")
    item = table(
        data(
            f"https://proxy.golang.org/{go_path(package)}/@v/{quote(value, safe='')}.info"
        ),
        "Go proxy version",
    )
    if item.get("Version") != value:
        raise ValueError("Go proxy version identity mismatch")
    # Go's Time is version-creation time, not registry publication time. This
    # public proxy metadata is not authenticated by the checksum database.
    return Release(value, timestamp(item.get("Time")))


def go_releases(
    package: str,
    available: list[str] | None = None,
    *,
    policy: Mapping[str, object] | None = None,
    now: datetime | None = None,
    bounds: tuple[str, ...] = (),
    exact: str | None = None,
) -> list[Release]:
    """Load only metadata needed to prove the highest mature permitted release."""
    if available is not None and (
        not isinstance(available, list)
        or any(
            not isinstance(value, str) or not go_version(value) for value in available
        )
    ):
        raise ValueError("Go did not return a canonical unretracted version inventory")
    restrictions = [validate_constraint("go", bound) for bound in bounds]
    if policy is not None:
        if now is None:
            raise ValueError("Go ranked selection requires a frozen eligibility time")
        restrictions.append(constraint("go", policy, package))
        minimum_safe("go", policy, package)
        minimum_age(policy)
    values = (
        fetch(f"https://proxy.golang.org/{go_path(package)}/@v/list", "text/plain")[0]
        .decode()
        .splitlines()
    )
    values = sorted(
        {
            value
            for value in values
            if go_version(value)
            and version("go", value) is not None
            and (available is None or value in available)
            and (exact is None or value == exact)
            and (policy is None or meets_safe_floor("go", policy, package, value))
            and all(compatible("go", value, bound) for bound in restrictions)
        },
        key=lambda value: (go_precedence(value), value),
        reverse=True,
    )
    result = []
    winner = None
    for value in values:
        # Canonical +incompatible metadata does not change SemVer precedence.
        # Read every tied candidate before ruling out lower versions.
        rank = go_precedence(value)
        if winner is not None and rank < winner:
            break
        # Errors for any potentially winning version remain fatal. Once the
        # highest mature safe release is proved, lower metadata cannot change
        # selection or the retirement of a security age exception.
        release = go_info(package, value)
        result.append(release)
        if (
            policy is not None
            and now is not None
            and maturity("go", [release], policy, package, now)
        ):
            winner = rank
    return result


def go_precedence(value: str) -> tuple[int, int, int]:
    rank = version("go", value)
    if rank is None:
        raise ValueError("Go ranking requires a stable release")
    return rank.major, rank.minor, rank.patch


def go_digest(value: str) -> str:
    try:
        raw = base64.b64decode(value.removeprefix("h1:"), validate=True)
    except (AttributeError, binascii.Error):
        raw = b""
    if not isinstance(value, str) or not value.startswith("h1:") or len(raw) != 32:
        raise ValueError("Missing or malformed Go h1 checksum")
    return "h1:" + base64.b64encode(raw).decode()


def go_artifacts(package: str, value: str) -> tuple[Artifact, ...]:
    release = go_info(package, value)
    escaped = go_path(package)
    body = fetch(
        f"https://sum.golang.org/lookup/{escaped}@{quote(value, safe='')}", "text/plain"
    )[0].decode()
    result = {}
    # The record body precedes the signed tree. Do not interpret the signature
    # as a locally verified transparency proof; Go performs native verification.
    lines = body.split("\n\n", 1)[0].splitlines()
    if not lines or not lines[0].isdigit():
        raise ValueError("Malformed Go checksum database record")
    for line in lines[1:]:
        parts = line.split()
        if (
            len(parts) != 3
            or parts[0] != package
            or parts[1] not in (value, value + "/go.mod")
        ):
            raise ValueError("Go checksum database identity mismatch")
        suffix = ".mod" if parts[1].endswith("/go.mod") else ".zip"
        if suffix in result:
            raise ValueError("Duplicate Go checksum database identity")
        result[suffix] = Artifact(
            f"https://proxy.golang.org/{escaped}/@v/{quote(value, safe='')}{suffix}",
            go_digest(parts[2]),
            release.published,
        )
    if set(result) != {".mod", ".zip"}:
        raise ValueError("Go checksum database lacks module or go.mod checksum")
    return tuple(result.values())


def google_maven_group(group: str) -> bool:
    return group in {"com.android", "com.google.testing.platform"} or group.startswith(
        ("androidx.", "com.android.")
    )


def maven_prefix(package: str, repository: str = "central") -> str:
    if not isinstance(package, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*:[A-Za-z0-9_.-]+", package
    ):
        raise ValueError("Unsupported Maven coordinates")
    group, name = package.split(":")
    if name in (".", ".."):
        raise ValueError("Unsupported Maven artifact name")
    bases = {
        "central": "https://repo.maven.apache.org/maven2",
        "google": "https://dl.google.com/dl/android/maven2",
        "plugins": "https://plugins.gradle.org/m2",
    }
    if repository not in bases or (
        repository == "google" and not google_maven_group(group)
    ):
        raise ValueError("Unsupported Maven repository or group scope")
    if repository == "plugins" and not name.endswith(
        ("-gradle-plugin", ".gradle.plugin")
    ):
        raise ValueError("Unsupported Gradle Plugin Portal coordinate scope")
    return f"{bases[repository]}/{group.replace('.', '/')}/{name}"


def maven_content_digest(url: str, modified: str) -> str:
    # Hash actual bytes when the repository has no corresponding SHA-256 sidecar.
    hashed, total = hashlib.sha256(), 0
    with urlopen(
        Request(url, headers={"User-Agent": "nix-just-toolchain"}), timeout=30
    ) as response:
        if response.headers.get("Last-Modified") != modified:
            raise ValueError(
                "Maven artifact changed between metadata and content requests"
            )
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > 512 * 1024 * 1024:
                raise ValueError("Maven artifact exceeds 512 MiB audit bound")
            hashed.update(chunk)
    return hashed.hexdigest()


def maven_artifact(
    package: str, value: str, filename: str, repository: str = "central"
) -> Artifact:
    if lock_version("maven", value) is None or not re.fullmatch(
        r"[A-Za-z0-9_.+-]+", filename
    ):
        raise ValueError("Unsupported Maven artifact identity")
    name = package.split(":")[1]
    if not filename.startswith(f"{name}-{value}.") and not filename.startswith(
        f"{name}-{value}-"
    ):
        raise ValueError("Maven artifact filename disagrees with component coordinates")
    url = f"{maven_prefix(package, repository)}/{value}/{filename}"
    _, headers = fetch(url, "application/octet-stream", "HEAD")
    modified = next(
        (v for k, v in headers.items() if k.lower() == "last-modified"), None
    )
    if not modified:
        raise ValueError("Maven artifact lacks publication-age metadata")
    at = parsedate_to_datetime(modified)
    if at.tzinfo is None:
        raise ValueError("Maven publication-age metadata lacks a timezone")
    if repository == "plugins":
        # Portal checksum URLs can redirect to Central even when the Portal
        # artifact has different bytes. Its own artifact bytes are authoritative.
        checksum = maven_content_digest(url, modified)
    else:
        try:
            checksum = fetch(url + ".sha256", "text/plain")[0].decode().strip()
        except RegistryHTTPError as exc:
            if exc.status != 404:
                raise
            checksum = maven_content_digest(url, modified)
    return Artifact(url, digest("sha256:" + checksum), at.astimezone(timezone.utc))


def maven_releases(package: str, repository: str = "central") -> list[Release]:
    prefix = maven_prefix(package, repository)
    xml = ET.fromstring(fetch(f"{prefix}/maven-metadata.xml", "application/xml")[0])
    result = []
    for item in xml.findall("./versioning/versions/version"):
        name = item.text or ""
        if version("maven", name) is None:
            continue
        artifact = maven_artifact(
            package, name, f"{package.split(':')[1]}-{name}.pom", repository
        )
        result.append(Release(name, artifact.published, artifacts=(artifact,)))
    return result


def docker_releases(
    repository: str, accepts_tag: Callable[[str], bool]
) -> list[Release]:
    """Discover dated tags; callers must validate the selected manifest identity."""
    if isinstance(repository, str):
        repository = repository.removeprefix("docker.io/")
    if not isinstance(repository, str) or not re.fullmatch(
        r"[a-z0-9_.-]+/[a-z0-9_.-]+", repository
    ):
        raise ValueError("Docker Hub requires an explicit namespace/repository")
    prefix = f"https://hub.docker.com/v2/repositories/{repository}/tags"
    url = prefix + "?page_size=100"
    result, visited = [], set()
    for _ in range(100):
        if url in visited or not url.startswith((prefix + "?", prefix + "/?")):
            raise ValueError("Unexpected Docker Hub pagination target")
        visited.add(url)
        body = data(url)
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise ValueError("Malformed Docker Hub tag inventory")
        for entry in body["results"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ValueError("Malformed Docker Hub tag entry")
            if not accepts_tag(entry["name"]):
                continue
            # Legacy inventory can lack a digest. Keep the candidate visible to
            # ranking so missing evidence cannot silently select an older tag.
            identity = entry.get("digest")
            result.append(
                Release(
                    entry["name"],
                    timestamp(entry.get("last_updated")),
                    "" if identity is None else digest(identity),
                )
            )
        next_url = body.get("next")
        if next_url is None:
            return result
        if not isinstance(next_url, str) or not next_url:
            raise ValueError("Unexpected Docker Hub pagination target")
        url = next_url
    raise ValueError("Docker Hub pagination exceeded its bound")


def releases(
    provider: str,
    package: str,
    *,
    include_prerelease: bool = False,
    include_deprecated: bool = False,
) -> list[Release]:
    if provider == "go":
        return go_releases(package)
    if provider == "swift":
        return swift_releases(package)
    if provider == "npm":
        body = table(
            data(f"https://registry.npmjs.org/{quote(package, safe='')}"), "npm package"
        )
        result = []
        dates = table(body.get("time", {}), "npm publication times")
        for value, raw_info in table(body["versions"], "npm versions").items():
            info = table(raw_info, "npm version")
            parsed = (
                lock_version(provider, value)
                if include_prerelease
                else version(provider, value)
            )
            deprecated = bool(info.get("deprecated"))
            if parsed is None or (deprecated and not include_deprecated):
                continue
            dist = table(info.get("dist", {}), "npm distribution")
            integrity = dist.get("integrity")
            # Older npm releases expose the registry's SHA-1 tarball checksum.
            if not integrity and re.fullmatch(
                r"[a-f0-9]{40}", text(dist.get("shasum", ""), "npm shasum")
            ):
                integrity = (
                    "sha1-"
                    + base64.b64encode(
                        bytes.fromhex(text(dist["shasum"], "npm shasum"))
                    ).decode()
                )
            artifact = Artifact(
                artifact_url(text(dist.get("tarball"), "npm tarball")),
                digest(text(integrity, "npm artifact digest"), npm=True),
                timestamp(dates.get(value)),
            )
            result.append(
                Release(
                    value,
                    artifact.published,
                    artifacts=(artifact,),
                    deprecated=deprecated,
                )
            )
        return result
    if provider == "crates":
        try:
            body = table(
                data(f"https://crates.io/api/v1/crates/{quote(package, safe='')}"),
                "Crate package",
            )
        except RegistryHTTPError as exc:
            # Identify ordinary crate names without exposing arbitrary URL-like inputs.
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", package):
                exc.package_context = f"crates:{package}"
            raise
        result = []
        for raw in array(body["versions"], "Crate versions"):
            crate = table(raw, "Crate version")
            number = text(crate["num"], "Crate version number")
            if crate["yanked"] or version(provider, number) is None:
                continue
            artifact = Artifact(
                f"https://crates.io/api/v1/crates/{quote(package, safe='')}/{number}/download",
                digest("sha256:" + text(crate.get("checksum", ""), "Crate checksum")),
                timestamp(crate.get("created_at")),
            )
            result.append(Release(number, artifact.published, artifacts=(artifact,)))
        return result
    if provider == "pypi":
        import platform

        body = table(
            data(f"https://pypi.org/pypi/{quote(package, safe='')}/json"),
            "PyPI package",
        )
        result = []
        for name, artifacts in table(body["releases"], "PyPI releases").items():
            records = [
                table(a, "PyPI artifact") for a in array(artifacts, "PyPI artifacts")
            ]
            available = [a for a in records if not a.get("yanked")]
            if version(provider, name) is not None and available:
                actual = tuple(
                    Artifact(
                        artifact_url(text(a.get("url"), "PyPI artifact URL")),
                        digest(
                            "sha256:"
                            + text(
                                table(a.get("digests", {}), "PyPI digests").get(
                                    "sha256", ""
                                ),
                                "PyPI SHA-256",
                            )
                        ),
                        timestamp(a.get("upload_time_iso_8601")),
                    )
                    for a in available
                )
                # Keep evidence for all locked platforms/Pythons. A release with
                # no host-compatible file is retained for audit, but not selection.
                supported = any(
                    not a.get("requires_python")
                    or PythonVersion(platform.python_version())
                    in SpecifierSet(
                        text(a["requires_python"], "PyPI Python constraint")
                    )
                    for a in available
                )
                result.append(
                    Release(
                        name,
                        max(a.published for a in actual),
                        python="" if supported else "unsupported",
                        artifacts=actual,
                    )
                )
        return result
    if provider == "pub":
        body = table(
            data(f"https://pub.dev/api/packages/{quote(package, safe='')}"),
            "Pub package",
        )
        result = []
        for raw in array(body["versions"], "Pub versions"):
            pub = table(raw, "Pub version")
            number = text(pub["version"], "Pub version number")
            if pub.get("retracted") or version(provider, number) is None:
                continue
            artifact = Artifact(
                artifact_url(text(pub.get("archive_url"), "Pub archive URL")),
                digest(
                    "sha256:"
                    + text(pub.get("archive_sha256", ""), "Pub archive SHA-256")
                ),
                timestamp(pub.get("published")),
            )
            result.append(Release(number, artifact.published, artifacts=(artifact,)))
        return result
    if provider == "github":
        return github_releases(package)
    if provider == "docker":
        return docker_releases(package, lambda tag: version(provider, tag) is not None)
    if provider == "maven":
        return maven_releases(package)
    raise ValueError(f"Unsupported registry provider: {provider}")
