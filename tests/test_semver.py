from config_center.semver import SemVer, compatible, parse_constraint

import pytest


def test_semver_ordering():
    assert SemVer.parse("1.2.3") < SemVer.parse("1.2.4")
    assert SemVer.parse("1.9.9") < SemVer.parse("2.0.0")
    assert SemVer.parse("2.0.0-alpha") < SemVer.parse("2.0.0")


def test_caret_and_tilde_ranges():
    assert compatible("^1.2.3", "1.9.0")
    assert not compatible("^1.2.3", "2.0.0")
    assert compatible("~1.2.3", "1.2.9")
    assert not compatible("~1.2.3", "1.3.0")


def test_compound_and_blank_constraint():
    assert compatible(">=1.2.0, <2.0.0", "1.5.0")
    assert not compatible(">=1.2.0, <2.0.0", "2.0.0")
    assert compatible("", "9.9.9")
    assert compatible("*", "0.0.1")


def test_bad_spec_raises():
    with pytest.raises(ValueError):
        parse_constraint("===1.0.0")
    with pytest.raises(ValueError):
        SemVer.parse("not-a-version")
