from hypothesis import given, strategies as st

from runtime.egress_proxy import host_allowed
from runtime.secrets import REDACTED, redact, register_secret


@given(st.text(min_size=6).filter(lambda value: "\x00" not in value))
def test_registered_secret_never_survives_recursive_redaction(secret):
    register_secret(secret)
    result = redact({"message": "prefix" + secret + "suffix", "token": secret})
    assert secret not in result["message"]
    assert result["token"] == REDACTED


@given(
    allowed=st.from_regex(r"[a-z]{1,12}\.example\.com", fullmatch=True),
    prefix=st.from_regex(r"[a-z]{1,8}", fullmatch=True),
)
def test_hostname_allowlist_never_grants_unlisted_subdomain(allowed, prefix):
    assert host_allowed(allowed, {allowed})
    assert not host_allowed(prefix + "." + allowed, {allowed})
