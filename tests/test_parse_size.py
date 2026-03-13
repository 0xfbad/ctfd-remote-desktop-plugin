from docker_host_manager import parse_size


def test_bytes_passthrough():
    assert parse_size("1024") == 1024


def test_kilobytes():
    assert parse_size("1k") == 1024


def test_megabytes():
    assert parse_size("1m") == 1024**2


def test_gigabytes():
    assert parse_size("4g") == 4 * 1024**3


def test_kb_suffix():
    assert parse_size("1kb") == 1024


def test_mb_suffix():
    assert parse_size("512mb") == 512 * 1024**2


def test_gb_suffix():
    assert parse_size("2gb") == 2 * 1024**3


def test_gb_before_g():
    # "gb" must match before "g" so "2gb" doesn't parse as "2g" + leftover "b"
    assert parse_size("1gb") == 1024**3
    assert parse_size("1g") == 1024**3


def test_case_insensitive():
    assert parse_size("4G") == 4 * 1024**3
    assert parse_size("512MB") == 512 * 1024**2


def test_whitespace_stripped():
    assert parse_size("  4g  ") == 4 * 1024**3


def test_float_truncated():
    assert parse_size("1.5g") == int(1.5 * 1024**3)


def test_numeric_input():
    assert parse_size(4096) == 4096


def test_zero():
    assert parse_size("0") == 0
