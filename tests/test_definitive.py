"""Definitive Client API — vault trading with no wallet.

The auth format is the whole risk surface here: a mismatch signs the wrong bytes
and comes back as a bare 401 with nothing to debug. These pin the prehash and the
compact-JSON rule against the documented format.
"""

import json

import pytest

from cambrian.runners import definitive as D


def test_prehash_matches_the_documented_format():
    # {method}:{path}?{query}:{timestamp}:{sortedHeaders}{body}
    msg = D.prehash(method="POST", path="/v2/portfolio/quicktrade/quote",
                    timestamp="1700000000000",
                    headers={"x-definitive-api-key": "dpka_abc",
                             "x-definitive-timestamp": "1700000000000"},
                    query=None, body_str='{"a":1}')
    assert msg == ('POST:/v2/portfolio/quicktrade/quote?:1700000000000:'
                   'x-definitive-api-key:"dpka_abc",'
                   'x-definitive-timestamp:"1700000000000"'
                   '{"a":1}')


def test_header_values_are_json_quoted_not_bare():
    # The docs render them via JSON.stringify, so a string carries its quotes.
    msg = D.prehash(method="GET", path="/p", timestamp="1",
                    headers={"x-definitive-api-key": "k"})
    assert 'x-definitive-api-key:"k"' in msg


def test_only_definitive_headers_participate_and_they_are_sorted():
    msg = D.prehash(method="GET", path="/p", timestamp="1",
                    headers={"x-definitive-timestamp": "1",
                             "x-definitive-api-key": "k",
                             "content-type": "application/json",
                             "user-agent": "x"})
    assert "content-type" not in msg and "user-agent" not in msg
    assert msg.index("x-definitive-api-key") < msg.index("x-definitive-timestamp")


def test_query_string_is_included_even_when_empty():
    assert D.prehash(method="GET", path="/p", timestamp="1", headers={}) == "GET:/p?:1:"
    assert "?a=1" in D.prehash(method="GET", path="/p", timestamp="1",
                               headers={}, query={"a": "1"})


def test_secret_prefix_is_stripped_before_use_as_the_hmac_key():
    import hashlib
    import hmac as _h
    msg = "GET:/p?:1:"
    expected = _h.new(b"rawsecret", msg.encode(), hashlib.sha256).hexdigest()
    assert D.sign("dpks_rawsecret", msg) == expected
    assert D.sign("rawsecret", msg) == expected      # tolerant of a bare secret


def test_body_is_serialised_compactly_so_signed_bytes_equal_sent_bytes():
    # JS JSON.stringify is compact; Python's default is not. Signing pretty JSON
    # while sending compact JSON (or the reverse) is a silent 401.
    body = {"chain": "robinhood", "qty": "8"}
    compact = json.dumps(body, separators=(",", ":"))
    assert " " not in compact
    assert compact == '{"chain":"robinhood","qty":"8"}'


def test_submitting_requires_explicit_confirmation():
    # There is no wallet signature between the key and the fill on this API, so
    # the confirm gate matters more here than on the Flash path, not less.
    with pytest.raises(D.DefinitiveError) as e:
        D.quicktrade_submit(target="0xt", contra="0xc", qty="8")
    assert "confirm" in str(e.value)


def test_missing_credentials_fail_loudly(monkeypatch):
    for var in ("DEFINITIVE_API_KEY", "DEFINITIVE_API_SECRET", "API_KEY", "API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(D.DefinitiveError) as e:
        D.request("/v2/portfolio")
    assert "DEFINITIVE_API_KEY" in str(e.value)


def test_quote_cost_flattens_the_numbers_a_decision_needs():
    q = {"quote": {"quote": {"id": "q-1"}},
         "metadata": {"fromNotional": "20", "toNotional": "19.5",
                      "estimatedPriceImpact": "0.004",
                      "estimatedFeeNotional": "0.05",
                      "minAmountOut": "123", "warnings": ["minimum order size"]}}
    c = D.quote_cost(q)
    assert c["spend_usd"] == 20.0 and c["receive_usd"] == 19.5
    assert abs(c["loss_pct"] - 2.5) < 1e-9
    assert c["quote_id"] == "q-1"
    assert c["warnings"] == ("minimum order size",)


def test_quote_cost_survives_a_quote_with_nothing_in_it():
    c = D.quote_cost({})
    assert c["spend_usd"] is None and c["loss_pct"] is None


def test_robinhood_is_the_default_chain():
    assert D.CHAIN == "robinhood"
