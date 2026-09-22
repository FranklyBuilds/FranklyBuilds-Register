from __future__ import annotations

import pytest

from backend.proxy_parser import (
    ProxyEntry,
    ProxyParseError,
    parse_proxy_pool,
    parse_proxy_text,
    proxy_input_tokens,
)


@pytest.mark.parametrize(
    ("raw", "scheme", "host", "port", "username", "password"),
    [
        ("http://proxy.example:8080", "http", "proxy.example", 8080, "", ""),
        ("HTTPS://user:pa%40ss@Proxy.Example:443", "https", "proxy.example", 443, "user", "pa@ss"),
        ("socks5://user:pass@proxy.example:1080", "socks5", "proxy.example", 1080, "user", "pass"),
        ("socks5h://proxy.example:1080", "socks5h", "proxy.example", 1080, "", ""),
        ("user:pass@proxy.example:8000", "http", "proxy.example", 8000, "user", "pass"),
        ("proxy.example:8000:user:pass", "http", "proxy.example", 8000, "user", "pass"),
        ("proxy.example:8000", "http", "proxy.example", 8000, "", ""),
        ("[2001:db8::1]:1080", "http", "2001:db8::1", 1080, "", ""),
        ("[2001:db8::1]:1080:user:pa:ss", "http", "2001:db8::1", 1080, "user", "pa:ss"),
        ("socks5h://user:p%40ss@[2001:db8::2]:1081", "socks5h", "2001:db8::2", 1081, "user", "p@ss"),
        ("socks://proxy.example:1080", "socks5", "proxy.example", 1080, "", ""),
        ("SOCKS://proxy.example:1080", "socks5", "proxy.example", 1080, "", ""),
    ],
)
def test_parse_supported_proxy_shapes(
    raw: str,
    scheme: str,
    host: str,
    port: int,
    username: str,
    password: str,
) -> None:
    entry = ProxyEntry.parse(raw)

    assert (entry.scheme, entry.host, entry.port) == (scheme, host, port)
    assert (entry.username, entry.password) == (username, password)


def test_urls_encode_credentials_and_bracket_ipv6() -> None:
    entry = ProxyEntry.parse("http://a%40b:p%3Ass@[2001:DB8::1]:8080")

    assert entry.direct_url == "http://a%40b:p%3Ass@[2001:db8::1]:8080"
    assert entry.masked == "http://***:***@[2001:db8::1]:8080"
    assert entry.to_resource_kwargs(country="JP", group="direct") == {
        "host": "2001:db8::1",
        "port": 8080,
        "username": "a@b",
        "password": "p:ss",
        "scheme": "http",
        "country": "JP",
        "group": "direct",
    }


def test_direct_constructor_normalizes_socks_alias() -> None:
    assert ProxyEntry("proxy.example", 1080, scheme="socks").scheme == "socks5"


def test_url_form_keeps_noncanonical_ipv6_text_consistent_with_shorthand() -> None:
    url_entry = ProxyEntry.parse("http://[2001:0db8:0:0::1]:8080")
    shorthand_entry = ProxyEntry.parse("[2001:0db8:0:0::1]:8080")

    assert url_entry.host == shorthand_entry.host == "2001:0db8:0:0::1"


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://proxy.example:21",
        "proxy.example",
        "proxy.example:0",
        "proxy.example:65536",
        "proxy.example:not-a-port",
        "2001:db8::1:1080",
        "[not-an-ipv6]:1080",
        "[fe80::1%]:1080",
        "[fe80::1%bad zone]:1080",
        "http://proxy.example:8080/path",
        "http://user:bad%2@proxy.example:8080",
        "http://proxy.example:8080?source=pool",
        "http://proxy.example:8080#fragment",
        "http://proxy.example:8080?",
        "http://proxy.example:8080#",
        "[proxy.example]:8080",
    ],
)
def test_invalid_proxy_shapes_raise_structured_error(raw: str) -> None:
    with pytest.raises(ProxyParseError) as raised:
        ProxyEntry.parse(raw)

    assert raised.value.code


def test_batch_parser_skips_comments_collects_errors_and_can_dedupe() -> None:
    result = parse_proxy_text(
        "\ufeff# comment\n"
        "// another comment\n"
        "proxy.example:8000\n"
        "proxy.example:8000\n"
        "bad-line\n"
        "\n",
        dedupe=True,
    )

    assert len(result) == 1
    assert result.skipped == 3
    assert result.duplicates == 1
    assert result.errors[0].code == "invalid_host_port"
    assert result.errors[0].line_number == 5
    assert result.total_lines == 6


def test_pool_helper_returns_only_usable_entries_and_supports_csv() -> None:
    entries = parse_proxy_pool("proxy.example,8080\nproxy2.example,8081,user,p@ss")

    assert [entry.direct_url for entry in entries] == [
        "http://proxy.example:8080",
        "http://user:p%40ss@proxy2.example:8081",
    ]


def test_csv_parser_preserves_spaces_and_rejects_stray_quotes() -> None:
    entry = ProxyEntry.parse('proxy.example,8080,user,"password with spaces"')
    assert (entry.host, entry.port, entry.username, entry.password) == (
        "proxy.example",
        8080,
        "user",
        "password with spaces",
    )

    with pytest.raises(ProxyParseError) as raised:
        ProxyEntry.parse('proxy.example,8080,user,"password"tail')
    assert raised.value.code == "invalid_csv"


def test_url_credential_percent_encoding_must_be_well_formed() -> None:
    with pytest.raises(ProxyParseError) as raised:
        ProxyEntry.parse("http://user:bad%2@proxy.example:8080")
    assert raised.value.code == "invalid_auth"


def test_proxy_input_tokens_keep_csv_records_intact() -> None:
    assert proxy_input_tokens('proxy.example,8080,user,"password with spaces"') == (
        'proxy.example,8080,user,"password with spaces"',
    )
    assert proxy_input_tokens("http://a.example:80 socks://b.example:81") == (
        "http://a.example:80",
        "socks://b.example:81",
    )
    assert proxy_input_tokens("  # comment") == ()


def test_strict_batch_mode_raises_first_error() -> None:
    with pytest.raises(ProxyParseError) as raised:
        parse_proxy_text("proxy.example:8000\nbad-line", strict=True)

    assert raised.value.line_number == 2
