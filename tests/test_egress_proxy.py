import pytest

from runtime.egress_proxy import host_allowed, normalize_host, parse_target


def test_host_allowlist_is_exact_and_case_insensitive():
    allowed = {"API.Example.com."}
    assert host_allowed("api.example.com", allowed)
    assert not host_allowed("evil.api.example.com", allowed)
    assert not host_allowed("example.com", allowed)


def test_normalize_host_removes_only_case_and_trailing_dot():
    assert normalize_host("Example.COM.") == "example.com"


def test_parse_connect_target():
    assert parse_target("CONNECT", "example.com:443") == (
        "example.com", 443, "example.com:443",
    )


def test_parse_absolute_http_target_to_origin_form():
    assert parse_target("GET", "http://example.com:8080/a?q=1") == (
        "example.com", 8080, "/a?q=1",
    )


@pytest.mark.parametrize("target", ["/relative", "file:///etc/passwd", "example.com"])
def test_parse_target_rejects_non_http_absolute_urls(target):
    with pytest.raises(ValueError):
        parse_target("GET", target)
