import pytest

from _rd_plugin.utils import normalize_public_hostname


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("runner.example.edu", "runner.example.edu"),
        ("RUNNER.Example.EDU", "runner.example.edu"),
        ("localhost", "localhost"),
        ("192.0.2.10", "192.0.2.10"),
        ("2001:0db8::1", "[2001:db8::1]"),
        ("[2001:0db8::1]", "[2001:db8::1]"),
    ],
)
def test_normalizes_valid_url_hosts(raw, expected):
    assert normalize_public_hostname(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        " runner.example.edu",
        "runner.example.edu ",
        "https://runner.example.edu",
        "runner.example.edu:443",
        "user@runner.example.edu",
        "runner.example.edu/path",
        "runner_example.edu",
        "runner.example.edu\nInjected",
        "[192.0.2.10]",
        "[2001:db8::1",
    ],
)
def test_rejects_non_host_url_components(raw):
    with pytest.raises(ValueError, match="pub_hostname"):
        normalize_public_hostname(raw)
