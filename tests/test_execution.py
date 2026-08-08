"""Execution layer: quote -> external signature -> submit.

The invariants worth protecting here are all safety ones. Nothing in this layer
signs, nothing accepts a private key, nothing fires without an explicit confirm,
and a quote that looks wrong stops the order rather than being passed along.
"""

import json

import pytest

from cambrian.runners import execution as X
from cambrian.runners import venues as V

MM = 10 ** 18


def _quote_payload(*, spend="19.17", recv="19.05", impact="0.0042",
                   typed='{"types":{}}', wrap=True):
    return {
        "quoteId": "q-1", "orderType": "market", "side": "buy",
        "from": {"asset": "contra", "amount": "0.01", "notional": spend},
        "to": {"asset": "target", "amount": "3187.9", "notional": recv},
        "estimatedPriceImpact": impact,
        "wrap": {"evmTx": {"to": "0xweth", "data": "0xd0e"}} if wrap else None,
        "evm": {"approveTx": {"to": "0xweth", "data": "0x095ea7b3"},
                "permitTypedData": "", "orderTypedData": typed},
    }


def _venue(**kw):
    base = dict(kind=V.PONS_V2, token="0xtok", pool="0xcurve",
                quote_token=V.NATIVE_ETH, pricing_reserves=(10 * MM, 1000 * MM),
                graduated=False)
    base.update(kw)
    return V.Venue(**base)


# --- routing ----------------------------------------------------------------

def test_fresh_pons_v2_curves_do_not_route_through_flash():
    # Flash returns "missing notional rates" for a bonding curve — it indexes
    # AMMs, not curves. Routing one to Flash would read as "no liquidity".
    assert X.route_for(_venue(graduated=False)) == X.ROUTE_CURVE


def test_graduated_and_pooled_venues_route_through_flash():
    assert X.route_for(_venue(graduated=True)) == X.ROUTE_FLASH
    for kind in (V.FLAP, V.UNI_V2, V.POOLS_TRADE, V.PONS_V1):
        assert X.route_for(_venue(kind=kind)) == X.ROUTE_FLASH


# --- preparing --------------------------------------------------------------

def test_prepare_returns_typed_data_and_never_a_key(monkeypatch):
    monkeypatch.setattr(X, "quote", lambda **kw: _quote_payload())
    p = X.prepare(target="0xt", contra="0xc", qty="0.01", funder="0xf")
    assert p.quote_id == "q-1"
    assert p.order_typed_data == '{"types":{}}'
    assert p.approve_tx["to"] == "0xweth"
    assert p.wrap_tx is not None            # native spend needs wrapping first
    # the object hands out what a signer needs and nothing more
    assert not any("key" in f for f in p.__dataclass_fields__)


def test_prepare_refuses_a_quote_that_loses_too_much(monkeypatch):
    # The quote is the first time we see what the venue will REALLY pay, so it
    # gets its own check rather than trusting the earlier tax gate.
    monkeypatch.setattr(X, "quote", lambda **kw: _quote_payload(spend="20", recv="17"))
    with pytest.raises(X.FlashError) as e:
        X.prepare(target="0xt", contra="0xc", qty="0.01", funder="0xf",
                  max_loss_pct=10.0)
    assert "refusing" in str(e.value)


def test_prepare_warns_but_proceeds_on_a_moderate_loss(monkeypatch):
    monkeypatch.setattr(X, "quote", lambda **kw: _quote_payload(spend="20", recv="18.8"))
    p = X.prepare(target="0xt", contra="0xc", qty="0.01", funder="0xf",
                  max_loss_pct=10.0)
    assert p.warnings and "loses" in p.warnings[0]


def test_prepare_fails_when_there_is_nothing_to_sign(monkeypatch):
    monkeypatch.setattr(X, "quote", lambda **kw: _quote_payload(typed=""))
    with pytest.raises(X.FlashError):
        X.prepare(target="0xt", contra="0xc", qty="0.01", funder="0xf")


def test_slippage_vs_quote_is_reported():
    p = X.PreparedOrder(quote_id="q", target="0xt", contra="0xc", side="buy",
                        qty="1", spend_notional_usd=100.0,
                        receive_notional_usd=95.0, price_impact=0.01,
                        order_typed_data="{}", permit_typed_data=None,
                        approve_tx=None, wrap_tx=None)
    assert abs(p.slippage_vs_quote_pct - 5.0) < 1e-9


# --- submitting -------------------------------------------------------------

def _prepared():
    return X.PreparedOrder(quote_id="q-1", target="0xt", contra="0xc", side="buy",
                           qty="0.01", spend_notional_usd=19.0,
                           receive_notional_usd=18.9, price_impact=0.004,
                           order_typed_data='{"types":{}}', permit_typed_data=None,
                           approve_tx=None, wrap_tx=None)


def test_submit_refuses_without_explicit_confirmation():
    # Everything upstream is reversible; this call is not.
    with pytest.raises(X.FlashError) as e:
        X.submit(_prepared(), funder="0xf", signature="0xabc")
    assert "confirm" in str(e.value)


def test_submit_requires_a_real_signature():
    for bad in ("", None, "not-hex"):
        with pytest.raises(X.FlashError):
            X.submit(_prepared(), funder="0xf", signature=bad, confirm=True)


def test_submit_echoes_the_quote_and_typed_data(monkeypatch):
    sent = {}
    monkeypatch.setattr(X, "_post", lambda path, body, **kw: sent.update(
        {"path": path, "body": body}) or {"orderId": "o-1"})
    out = X.submit(_prepared(), funder="0xf", signature="0x" + "ab" * 32,
                   confirm=True)
    assert out["orderId"] == "o-1"
    b = sent["body"]
    assert b["quoteId"] == "q-1"                     # binds to the priced quote
    assert b["evmOrderTypedData"] == '{"types":{}}'  # echo is required by the API
    assert b["userSignature"].startswith("0x")
    assert b["targetChain"] == "robinhood"
    assert "privateKey" not in json.dumps(b)


# --- live-API lessons -------------------------------------------------------

def test_requests_send_a_browser_user_agent():
    # Without one, Cloudflare answers 403 "error code: 1010", which reads exactly
    # like an auth failure and sends you hunting for a bad API key.
    assert "Mozilla/5.0" in X._UA


def test_eth_price_comes_from_the_quotes_own_notionals():
    # The repo default of $3000 measured ~1917 live — a 56% overstatement that
    # would inflate every market cap and every position sized from one.
    assert abs(X.eth_usd_from_quote(_quote_payload()) - 1917.0) < 1.0
    assert X.eth_usd_from_quote({"from": {"amount": "0", "notional": "1"}}) is None
    assert X.eth_usd_from_quote({}) is None


def test_flash_errors_carry_the_servers_own_code():
    e = X.FlashError("missing notional rates", code="INVALID_ARGUMENT", status=400)
    assert e.code == "INVALID_ARGUMENT" and e.status == 400
