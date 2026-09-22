"""Pure proxy-input parsing for the local resource pool.

The parser deliberately has no network, browser, or proxy-bridge side effects.
It accepts the formats emitted by common proxy providers and returns the same
split fields consumed by :meth:`ResourceService.upsert_proxy`.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Iterator, Sequence
from urllib.parse import quote, unquote, urlsplit, urlunsplit


SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
DEFAULT_PROXY_SCHEME = "http"
_PORT_RE = re.compile(r"^[0-9]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ProxyParseError(ValueError):
    """A structured error raised for one malformed proxy entry."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        line_number: int | None = None,
    ) -> None:
        self.code = code
        self.line_number = line_number
        self.message = message
        prefix = f"line {line_number}: " if line_number is not None else ""
        super().__init__(f"{prefix}{message} [{code}]")


@dataclass(frozen=True, slots=True)
class ProxyEntry:
    """A normalized proxy node suitable for the existing resource store."""

    host: str
    port: int
    username: str = ""
    password: str = ""
    scheme: str = DEFAULT_PROXY_SCHEME

    def __post_init__(self) -> None:
        scheme = str(self.scheme or DEFAULT_PROXY_SCHEME).strip().lower()
        if scheme == "socks":
            scheme = "socks5"
        if scheme not in SUPPORTED_PROXY_SCHEMES:
            raise ValueError(f"unsupported proxy scheme: {scheme}")
        host = str(self.host or "").strip().lower()
        _validate_host(host)
        port = _coerce_port(self.port)
        object.__setattr__(self, "scheme", scheme)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "username", str(self.username or ""))
        object.__setattr__(self, "password", str(self.password or ""))

    @classmethod
    def parse(
        cls,
        raw: str,
        *,
        default_scheme: str = DEFAULT_PROXY_SCHEME,
        line_number: int | None = None,
    ) -> "ProxyEntry":
        """Parse one URL, shorthand, or provider-style proxy line.

        Supported examples include ``host:port``,
        ``host:port:user:pass``, ``user:pass@host:port``, standard proxy URLs,
        CSV ``host,port[,user,password]``, and bracketed IPv6 forms.
        """

        text = _clean_input(raw)
        if not text:
            raise _error("empty", "proxy entry is empty", line_number)
        if text.startswith(("#", "//")):
            raise _error("comment", "proxy entry is a comment", line_number)

        scheme = _normalize_scheme(default_scheme, line_number=line_number)
        url_text = _coerce_to_url(text, scheme, line_number=line_number)
        try:
            parsed = urlsplit(url_text)
            parsed_scheme = parsed.scheme.lower()
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            message = str(exc).lower()
            code = "invalid_port" if "port" in message else "invalid_url"
            raise _error(code, "proxy URL is malformed", line_number) from exc

        if parsed_scheme not in SUPPORTED_PROXY_SCHEMES:
            raise _error(
                "unsupported_scheme",
                f"unsupported proxy scheme: {parsed_scheme or '<empty>'}",
                line_number,
            )
        if not host:
            raise _error("missing_host", "proxy host is required", line_number)
        if port is None:
            raise _error("missing_port", "proxy port is required", line_number)
        # Check the raw URL too: urlsplit intentionally drops empty ``?`` / ``#``
        # markers, but those are still URL suffixes and are not proxy config.
        if (
            parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or ("?" in text or "#" in text)
        ):
            raise _error(
                "invalid_url",
                "proxy URL must not contain a path, query, or fragment",
                line_number,
            )

        # Brackets are reserved for IPv6 literals in an authority.  Without
        # this check ``urlsplit`` would otherwise accept ``[hostname]:80`` and
        # silently turn it into a non-IPv6 host.
        bracketed = re.search(r"\[([^\]]+)\]", parsed.netloc)
        if bracketed:
            candidate = bracketed.group(1).split("%", 1)[0]
            try:
                ipaddress.IPv6Address(candidate)
            except ValueError as exc:
                raise _error(
                    "invalid_ipv6",
                    "bracketed proxy hosts must be valid IPv6 literals",
                    line_number,
                ) from exc

        normalized_host = host.strip().lower()
        _validate_host(normalized_host, line_number=line_number)
        try:
            username = _decode_url_credential(parsed.username or "")
            password = _decode_url_credential(parsed.password or "")
        except ValueError as exc:
            raise _error(
                "invalid_auth",
                "proxy authentication contains invalid URL encoding",
                line_number,
            ) from exc
        return cls(
            host=normalized_host,
            port=port,
            username=username,
            password=password,
            scheme=parsed_scheme,
        )

    @property
    def direct_url(self) -> str:
        """Return a URL with credentials encoded for HTTP clients."""

        host = _format_host(self.host)
        auth = ""
        if self.username or self.password:
            auth = quote(self.username, safe="")
            if self.password or self.username:
                auth += ":" + quote(self.password, safe="")
            auth += "@"
        return urlunsplit((self.scheme, f"{auth}{host}:{self.port}", "", "", ""))

    @property
    def url(self) -> str:
        """Compatibility alias used by the standalone registration tool."""

        return self.direct_url

    @property
    def masked(self) -> str:
        """Return a credential-redacted URL for logs and UI labels."""

        host = _format_host(self.host)
        auth = ""
        if self.username or self.password:
            auth = "***"
            if self.password or self.username:
                auth += ":***"
            auth += "@"
        return urlunsplit((self.scheme, f"{auth}{host}:{self.port}", "", "", ""))

    @property
    def uses_bridge(self) -> bool:
        """This pure adapter never starts or requires a local bridge."""

        return False

    def to_resource_kwargs(
        self,
        *,
        country: str | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        """Return keyword arguments accepted by ``ResourceService.upsert_proxy``."""

        result: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "scheme": self.scheme,
        }
        if country is not None:
            result["country"] = country
        if group is not None:
            result["group"] = group
        return result

    def to_dict(self, *, redact: bool = False) -> dict[str, Any]:
        """Return a stable mapping for API/import code."""

        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "username": "***" if redact and self.username else self.username,
            "password": "***" if redact and self.password else self.password,
            "url": self.masked if redact else self.direct_url,
        }


@dataclass(frozen=True, slots=True)
class ProxyParseIssue:
    """A non-fatal line parsing failure from :func:`parse_proxy_text`."""

    line_number: int
    code: str
    message: str
    raw: str = ""


@dataclass(frozen=True, slots=True)
class ProxyParseResult(Sequence[ProxyEntry]):
    """Batch parsing result that also behaves like a read-only sequence."""

    entries: tuple[ProxyEntry, ...]
    errors: tuple[ProxyParseIssue, ...] = ()
    skipped: int = 0
    duplicates: int = 0

    @property
    def total_lines(self) -> int:
        return len(self.entries) + len(self.errors) + self.skipped + self.duplicates

    @property
    def usable(self) -> int:
        return len(self.entries)

    def __getitem__(self, index: int | slice) -> ProxyEntry | tuple[ProxyEntry, ...]:
        return self.entries[index]

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[ProxyEntry]:
        return iter(self.entries)

    def to_resource_kwargs(
        self,
        *,
        country: str | None = None,
        group: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            entry.to_resource_kwargs(country=country, group=group)
            for entry in self.entries
        ]


def parse_proxy_entry(
    raw: str,
    *,
    default_scheme: str = DEFAULT_PROXY_SCHEME,
    line_number: int | None = None,
) -> ProxyEntry:
    """Functional alias for :meth:`ProxyEntry.parse`."""

    return ProxyEntry.parse(
        raw,
        default_scheme=default_scheme,
        line_number=line_number,
    )


def parse_proxy_line(
    raw: str,
    *,
    default_scheme: str = DEFAULT_PROXY_SCHEME,
    line_number: int | None = None,
) -> ProxyEntry | None:
    """Parse a line, returning ``None`` for blanks and full-line comments."""

    text = _clean_input(raw)
    if not text or text.startswith(("#", "//")):
        return None
    return parse_proxy_entry(
        text,
        default_scheme=default_scheme,
        line_number=line_number,
    )


def parse_proxy_text(
    raw_text: str,
    *,
    default_scheme: str = DEFAULT_PROXY_SCHEME,
    dedupe: bool = False,
    strict: bool = False,
) -> ProxyParseResult:
    """Parse a newline-separated proxy pool without doing network checks.

    Invalid lines are collected in ``errors`` unless ``strict`` is true.  A
    duplicate is only removed when ``dedupe`` is explicitly requested.
    """

    entries: list[ProxyEntry] = []
    errors: list[ProxyParseIssue] = []
    seen: set[tuple[str, int, str, str, str]] = set()
    skipped = duplicates = 0
    for line_number, raw_line in enumerate(str(raw_text or "").splitlines(), 1):
        text = _clean_input(raw_line)
        if not text or text.startswith(("#", "//")):
            skipped += 1
            continue
        try:
            entry = parse_proxy_entry(
                text,
                default_scheme=default_scheme,
                line_number=line_number,
            )
        except ProxyParseError as exc:
            if strict:
                raise
            errors.append(
                ProxyParseIssue(
                    line_number=line_number,
                    code=exc.code,
                    message=exc.message,
                    raw=text,
                )
            )
            continue
        key = (entry.scheme, entry.host, entry.port, entry.username, entry.password)
        if dedupe and key in seen:
            duplicates += 1
            continue
        seen.add(key)
        entries.append(entry)
    return ProxyParseResult(tuple(entries), tuple(errors), skipped, duplicates)


def parse_proxy_lines(
    raw_text: str,
    *,
    default_scheme: str = DEFAULT_PROXY_SCHEME,
    dedupe: bool = False,
    strict: bool = False,
) -> ProxyParseResult:
    """Alias named after the line-oriented import API."""

    return parse_proxy_text(
        raw_text,
        default_scheme=default_scheme,
        dedupe=dedupe,
        strict=strict,
    )


def parse_proxy_pool(
    raw_text: str,
    *,
    default_scheme: str = DEFAULT_PROXY_SCHEME,
    dedupe: bool = False,
    strict: bool = False,
) -> list[ProxyEntry]:
    """Return only usable entries from a proxy pool."""

    return list(
        parse_proxy_text(
            raw_text,
            default_scheme=default_scheme,
            dedupe=dedupe,
            strict=strict,
        ).entries
    )


def proxy_input_tokens(raw_line: str) -> tuple[str, ...]:
    """Split one import line without breaking quoted CSV credentials.

    Providers sometimes put several URL nodes on one whitespace-separated
    line.  A comma, however, denotes one CSV record and spaces inside a quoted
    field belong to that record.  The API importer and the UI use this same
    distinction.
    """

    text = _clean_input(raw_line)
    if not text or text.startswith(("#", "//")):
        return ()
    if "," in text:
        return (text,)
    return tuple(token for token in re.split(r"\s+", text) if token)


def _clean_input(raw: object) -> str:
    return str(raw or "").lstrip("\ufeff").strip()


def _parse_csv_fields(value: str) -> list[str] | None:
    """Parse one CSV record using the same quote rules as the UI parser."""

    fields: list[str] = []
    current: list[str] = []
    quoted = False
    quote_closed = False
    index = 0
    while index < len(value):
        character = value[index]
        if quoted:
            if character == '"':
                if index + 1 < len(value) and value[index + 1] == '"':
                    current.append('"')
                    index += 2
                    continue
                quoted = False
                quote_closed = True
            else:
                current.append(character)
            index += 1
            continue

        if character == '"':
            if "".join(current).strip():
                return None
            quoted = True
            index += 1
            continue
        if character == ",":
            fields.append("".join(current).strip())
            current = []
            quote_closed = False
            index += 1
            continue
        if quote_closed and not character.isspace():
            return None
        current.append(character)
        index += 1

    if quoted:
        return None
    fields.append("".join(current).strip())
    return fields


def _decode_url_credential(value: str) -> str:
    """Decode URL userinfo while rejecting malformed percent escapes."""

    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise ValueError("invalid percent escape")
    return unquote(value)


def _normalize_scheme(value: object, *, line_number: int | None = None) -> str:
    scheme = str(value or DEFAULT_PROXY_SCHEME).strip().lower()
    if scheme == "socks":
        scheme = "socks5"
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise _error("unsupported_scheme", f"unsupported proxy scheme: {scheme}", line_number)
    return scheme


def _coerce_to_url(text: str, default_scheme: str, *, line_number: int | None) -> str:
    """Turn shorthand forms into a URL while preserving IPv6 brackets."""

    if "://" in text:
        scheme, rest = text.split("://", 1)
        normalized_scheme = _normalize_scheme(scheme, line_number=line_number)
        return f"{normalized_scheme}://{rest}"

    # CSV exports are common and do not conflict with URL syntax.
    if "," in text:
        fields = _parse_csv_fields(text)
        if fields is None:
            raise _error("invalid_csv", "proxy CSV is malformed", line_number)
        if len(fields) == 2:
            host, port = fields
            return _build_url(default_scheme, host, port, "", "", line_number=line_number)
        if len(fields) == 4:
            host, port, username, password = fields
            return _build_url(
                default_scheme,
                host,
                port,
                username,
                password,
                line_number=line_number,
            )
        raise _error("invalid_csv", "proxy CSV must contain 2 or 4 fields", line_number)

    if "@" in text:
        return f"{default_scheme}://{text}"

    if text.startswith("["):
        match = re.fullmatch(
            r"\[(?P<host>[^\]]+)\]:(?P<port>[0-9]+)(?::(?P<user>[^:]*):(?P<password>.*))?",
            text,
        )
        if not match:
            raise _error("invalid_host_port", "bracketed IPv6 proxy format is invalid", line_number)
        try:
            ipaddress.IPv6Address((match.group("host") or "").split("%", 1)[0])
        except ValueError as exc:
            raise _error(
                "invalid_ipv6",
                "bracketed proxy hosts must be valid IPv6 literals",
                line_number,
            ) from exc
        return _build_url(
            default_scheme,
            match.group("host"),
            match.group("port"),
            match.group("user") or "",
            match.group("password") or "",
            line_number=line_number,
        )

    parts = text.split(":", 3)
    if len(parts) == 2:
        host, port = parts
        return _build_url(default_scheme, host, port, "", "", line_number=line_number)
    if len(parts) == 4:
        host, port, username, password = parts
        return _build_url(
            default_scheme,
            host,
            port,
            username,
            password,
            line_number=line_number,
        )
    raise _error(
        "invalid_host_port",
        "proxy format must be host:port or host:port:user:pass",
        line_number,
    )


def _build_url(
    scheme: str,
    host: str,
    port: str | int,
    username: str,
    password: str,
    *,
    line_number: int | None,
) -> str:
    host = str(host or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    _validate_host(host, line_number=line_number)
    port_text = str(port or "").strip()
    _coerce_port(port_text, line_number=line_number)
    auth = ""
    if username or password:
        auth = f"{quote(str(username).strip(), safe='')}:{quote(str(password).strip(), safe='')}@"
    return f"{scheme}://{auth}{_format_host(host)}:{port_text}"


def _format_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _validate_host(host: str, *, line_number: int | None = None) -> None:
    value = str(host or "").strip()
    if not value:
        raise _error("missing_host", "proxy host is required", line_number)
    if _CONTROL_RE.search(value) or any(char.isspace() for char in value):
        raise _error("invalid_host", "proxy host contains whitespace or control characters", line_number)
    if any(char in value for char in "/?#@[]\\^|<>"):
        raise _error("invalid_host", "proxy host contains invalid characters", line_number)
    if ":" in value:
        # A colon in a host is only valid for an IPv6 literal.  Zone IDs are
        # accepted when they use the conventional interface-name alphabet.
        candidate, _, zone = value.partition("%")
        if "%" in value and not re.fullmatch(r"[A-Za-z0-9_.-]+", zone):
            raise _error("invalid_ipv6", "IPv6 proxy zone ID is invalid", line_number)
        try:
            ipaddress.IPv6Address(candidate)
        except ValueError as exc:
            raise _error("invalid_ipv6", "IPv6 proxy hosts must be valid literals", line_number) from exc
    elif "%" in value:
        raise _error("invalid_host", "proxy host contains invalid characters", line_number)


def _coerce_port(value: object, *, line_number: int | None = None) -> int:
    text = str(value or "").strip()
    if not _PORT_RE.fullmatch(text):
        raise _error("invalid_port", "proxy port must be numeric", line_number)
    port = int(text)
    if not 1 <= port <= 65535:
        raise _error("invalid_port", "proxy port must be between 1 and 65535", line_number)
    return port


def _error(code: str, message: str, line_number: int | None) -> ProxyParseError:
    return ProxyParseError(code, message, line_number=line_number)


__all__ = [
    "DEFAULT_PROXY_SCHEME",
    "SUPPORTED_PROXY_SCHEMES",
    "ProxyEntry",
    "ProxyParseError",
    "ProxyParseIssue",
    "ProxyParseResult",
    "parse_proxy_entry",
    "parse_proxy_line",
    "parse_proxy_lines",
    "parse_proxy_pool",
    "parse_proxy_text",
    "proxy_input_tokens",
]
