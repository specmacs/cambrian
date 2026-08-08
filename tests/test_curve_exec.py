"""Direct execution against Pons v2 curves — the path Flash cannot route.

Everything here builds UNSIGNED transactions. The invariants under test are the
ones that cost money if wrong: correct calldata, a real minOut on every trade,
native-vs-ERC20 handled per launch, and a blocked plan never reaching calldata.
"""

import pytest

from cambrian.runners import curve_exec as CX
from cambrian.runners import venues as V

MM = 10 ** 18
REC = "0x00000000000000000000000000000000000000AA"
STOCK = "0x00000000000000000000000000000000000000BB"


def _curve(**kw) -> V.Venue:
    base = dict(kind=V.PONS_V2, token="0x00000000000000000000000000000000000000CC",
                pool="0x00000000000000000000000000000000000000DD",
                quote_token=V.NATIVE_ETH, quote_decimals=18, token_decimals=18,
                pricing_reserves=(10 * MM, 1000 * MM), fee_bps=100, tax_bps=0,
                total_supply=10**27, graduated=False, sellable_tokens=10**30)
    base.update(kw)
    return V.Venue(**base)


def _keccak(data: bytes) -> bytes:
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "examples" / "rh_codehash.py"
    text = src.read_text()
    ns: dict = {}
    exec(text[text.index("_M = (1 << 64)"):text.index("def rpc(")], ns)
    return ns["kec"](data)


def test_selectors_match_the_documented_signatures():
    # Both were also confirmed present in live curve bytecode.
    assert CX.SEL_BUY == "0x" + _keccak(b"buy(uint256,uint256,address)").hex()[:8]
    assert CX.SEL_SELL == "0x" + _keccak(b"sell(uint256,uint256,address)").hex()[:8]
    assert CX.SEL_APPROVE == "0x" + _keccak(b"approve(address,uint256)").hex()[:8]


def test_calldata_is_selector_plus_three_words():
    data = CX.encode_buy(10**18, 5 * 10**18, REC)
    assert data.startswith(CX.SEL_BUY)
    assert len(data) == 10 + 3 * 64
    assert data.endswith("aa")               # recipient right-aligned in word 3
    assert f"{10**18:064x}" in data


# --- native vs ERC-20 quote --------------------------------------------------

def test_native_launch_sends_value_and_needs_no_approval():
    # quoteIn must EQUAL msg.value; an approval here would be a wasted tx.
    txs = CX.build_buy(_curve(), quote_in=10**17, recipient=REC)
    assert len(txs) == 1
    assert txs[0].value == 10**17
    assert txs[0].to == _curve().pool


def test_erc20_quoted_launch_approves_first_and_sends_no_value():
    # ~60% of v2 launches are stock-paired. Sending value on one of those strands
    # ETH in the call.
    txs = CX.build_buy(_curve(quote_token=STOCK), quote_in=10**17, recipient=REC)
    assert len(txs) == 2
    assert txs[0].to == STOCK and txs[0].data.startswith(CX.SEL_APPROVE)
    assert txs[1].value == 0 and txs[1].data.startswith(CX.SEL_BUY)


def test_existing_allowance_skips_the_approval():
    class C:
        def eth_call(self, to, data):
            return hex(10**30)               # already approved plenty
    txs = CX.build_buy(_curve(quote_token=STOCK), quote_in=10**17, recipient=REC,
                       client=C(), owner=REC)
    assert len(txs) == 1 and txs[0].data.startswith(CX.SEL_BUY)


def test_insufficient_allowance_still_approves():
    class C:
        def eth_call(self, to, data):
            return hex(1)
    txs = CX.build_buy(_curve(quote_token=STOCK), quote_in=10**17, recipient=REC,
                       client=C(), owner=REC)
    assert len(txs) == 2


# --- minOut ------------------------------------------------------------------

def test_min_out_is_derived_from_our_own_quote():
    v = _curve()
    expected = V.quote_buy(v, 10**17)
    txs = CX.build_buy(v, quote_in=10**17, recipient=REC, slippage_bps=300)
    assert f"{expected * 9700 // 10000:064x}" in txs[0].data


def test_a_trade_is_never_built_with_a_zero_min_out():
    # minOut bounds the price; 0 is a standing invitation to be sandwiched, and a
    # fresh curve is exactly where that is cheapest to do.
    unpriceable = _curve(pricing_reserves=None)
    with pytest.raises(CX.CurveExecError) as e:
        CX.build_buy(unpriceable, quote_in=10**17, recipient=REC)
    assert "minOut" in str(e.value)


def test_full_slippage_tolerance_is_refused():
    with pytest.raises(CX.CurveExecError):
        CX.build_buy(_curve(), quote_in=10**17, recipient=REC, slippage_bps=10_000)


def test_tighter_slippage_raises_the_floor():
    loose = CX.build_buy(_curve(), quote_in=10**17, recipient=REC, slippage_bps=500)
    tight = CX.build_buy(_curve(), quote_in=10**17, recipient=REC, slippage_bps=50)
    assert loose[0].data != tight[0].data


# --- selling -----------------------------------------------------------------

def test_selling_always_approves_the_token():
    # The curve pulls tokens from the seller regardless of the quote asset.
    txs = CX.build_sell(_curve(), tokens_in=10**18, recipient=REC)
    assert len(txs) == 2
    assert txs[0].to == _curve().token
    assert txs[1].data.startswith(CX.SEL_SELL) and txs[1].value == 0


# --- routing guards ----------------------------------------------------------

def test_graduated_curves_are_pushed_back_to_flash():
    with pytest.raises(CX.CurveExecError) as e:
        CX.build_buy(_curve(graduated=True), quote_in=10**17, recipient=REC)
    assert "Flash" in str(e.value)


def test_non_curve_venues_are_rejected():
    for kind in (V.FLAP, V.UNI_V2, V.POOLS_TRADE):
        with pytest.raises(CX.CurveExecError):
            CX.build_buy(_curve(kind=kind), quote_in=10**17, recipient=REC)


# --- the gate stays binding --------------------------------------------------

def test_a_blocked_plan_never_reaches_calldata():
    # Otherwise the tax gate would be advisory rather than binding.
    plan = {"ok": False, "reasons": ["tax 10% > 3%"], "quote_in": 10**17}
    with pytest.raises(CX.CurveExecError) as e:
        CX.plan_to_txs(_curve(), plan, recipient=REC)
    assert "tax 10%" in str(e.value)


def test_an_approved_plan_becomes_transactions():
    plan = {"ok": True, "reasons": [], "quote_in": 10**17}
    txs = CX.plan_to_txs(_curve(), plan, recipient=REC)
    assert txs and txs[-1].data.startswith(CX.SEL_BUY)


def test_unsigned_txs_carry_no_key_material():
    txs = CX.build_buy(_curve(), quote_in=10**17, recipient=REC)
    fields = set(txs[0].__dataclass_fields__)
    assert fields == {"to", "data", "value", "note"}
    assert txs[0].as_dict()["value"].startswith("0x")
