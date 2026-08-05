import math

import pytest

from cambrian.units import (addr_eq, addr_in, bps_to_fraction, impermanent_loss,
                            scale_down, scale_up)


def test_bps_roundtrip():
    assert bps_to_fraction(300) == 0.03


def test_addr_eq_is_case_insensitive():
    assert addr_eq("0xABC", "0xabc")
    assert not addr_eq("0xABC", "0xdef")
    assert not addr_eq(None, "0xabc")


def test_addr_in_handles_dict_set_list():
    assert addr_in("0xABC", {"0xabc": "name"})
    assert addr_in("0xABC", {"0xabc"})
    assert addr_in("0xABC", ["0xabc"])
    assert not addr_in("0xABC", {"0xdef": "name"})
    assert not addr_in(None, {"0xabc"})


def test_scale_roundtrip():
    assert scale_up(1.5, 6) == 1_500_000
    assert scale_down(1_500_000, 6) == 1.5


def test_impermanent_loss_is_positive_and_symmetric():
    up = impermanent_loss(1.25)
    down = impermanent_loss(1 / 1.25)
    assert up > 0
    assert math.isclose(up, down, rel_tol=1e-9)
    # Known value for a 1.25x move: ~0.62%.
    assert math.isclose(up, 0.006192, abs_tol=1e-5)


def test_impermanent_loss_rejects_nonpositive():
    with pytest.raises(ValueError):
        impermanent_loss(0)
