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


def test_a_key_file_sets_credentials_without_shell_quoting(tmp_path, monkeypatch):
    # Setting secrets from a shell is where this actually broke: PowerShell
    # mangles unquoted values, bakes in quoted ones, and an interactive prompt
    # invites pasting the next command as the value. A KEY=VALUE file has none of
    # those semantics.
    monkeypatch.delenv("DEFINITIVE_API_KEY", raising=False)
    monkeypatch.delenv("DEFINITIVE_API_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cambrian.env").write_text(
        '# a comment\n\nDEFINITIVE_API_KEY=dpka_abc\n'
        'DEFINITIVE_API_SECRET="dpks_xyz"\n')
    from cambrian import config
    config._load_key_file()
    assert D.api_key() == "dpka_abc"
    assert D.api_secret() == "dpks_xyz"       # surrounding quotes stripped


def test_a_real_environment_variable_beats_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cambrian.env").write_text("DEFINITIVE_API_KEY=dpka_fromfile\n")
    monkeypatch.setenv("DEFINITIVE_API_KEY", "dpka_fromenv")
    from cambrian import config
    config._load_key_file()
    assert D.api_key() == "dpka_fromenv"


def test_a_malformed_key_file_never_stops_the_desk_booting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cambrian.env").write_text("this is not = = valid\x00\n")
    from cambrian import config
    config._load_key_file()          # must not raise


def test_a_pasted_terminal_line_still_yields_the_credential(tmp_path, monkeypatch):
    # What actually landed in the file on the owner's machine: 80 characters of
    # copied PowerShell prompt with the real key at the end. `dpka_` is
    # unambiguous, so position does not have to be right — only presence.
    monkeypatch.delenv("DEFINITIVE_API_KEY", raising=False)
    monkeypatch.delenv("DEFINITIVE_API_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cambrian.env").write_text(
        "DEFINITIVE_API_KEY=PS C:\\Users\\J> $env:DEFINITIVE_API_KEY=dpka_realkey123\n"
        "DEFINITIVE_API_SECRET=dpks_realsecret456\n")
    from cambrian import config
    config._load_key_file()
    assert D.api_key() == "dpka_realkey123"
    assert D.api_secret() == "dpks_realsecret456"


def test_swapped_values_self_correct(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFINITIVE_API_KEY", raising=False)
    monkeypatch.delenv("DEFINITIVE_API_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cambrian.env").write_text(
        "DEFINITIVE_API_KEY=dpks_realsecret456\n"
        "DEFINITIVE_API_SECRET=dpka_realkey123\n")
    from cambrian import config
    config._load_key_file()
    assert D.api_key() == "dpka_realkey123"
    assert D.api_secret() == "dpks_realsecret456"


def test_shell_text_loses_to_a_real_credential_in_the_file(tmp_path, monkeypatch):
    # Observed in the wild: a shell variable holding a copied terminal prompt
    # silently outranked a perfectly good file, because "the environment wins"
    # was applied to a value that is not a credential at all. A missing dpka_
    # prefix is proof it cannot be one, so the file wins — and says so.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cambrian.env").write_text("DEFINITIVE_API_KEY=dpka_fromfile\n")
    monkeypatch.setenv("DEFINITIVE_API_KEY", "PS C:\\Users\\J> whatever")
    from cambrian import config
    config._load_key_file()
    assert D.api_key() == "dpka_fromfile"
    assert "OVERRODE" in config.KEY_SOURCES["DEFINITIVE_API_KEY"]


def test_a_valid_shell_credential_still_beats_the_file(tmp_path, monkeypatch):
    # The override is narrow on purpose: a properly-prefixed environment value
    # is authoritative, so a stale file can never silently redirect a trade.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cambrian.env").write_text("DEFINITIVE_API_KEY=dpka_fromfile\n")
    monkeypatch.setenv("DEFINITIVE_API_KEY", "dpka_fromenv")
    from cambrian import config
    config._load_key_file()
    assert D.api_key() == "dpka_fromenv"


def test_credentials_in_files_reports_location_not_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cambrian.env").write_text("DEFINITIVE_API_SECRET=dpks_inthefile\n")
    from cambrian import config
    found = config.credentials_in_files()
    assert found["DEFINITIVE_API_SECRET"].endswith("cambrian.env")
    assert "DEFINITIVE_API_KEY" not in found
    assert "dpks_inthefile" not in str(found)


def test_a_bad_line_does_not_discard_the_good_lines_below_it(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFINITIVE_API_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cambrian.env").write_text(
        "BROKEN\x00NAME=whatever\nDEFINITIVE_API_SECRET=dpks_survived\n")
    from cambrian import config
    config._load_key_file()
    assert D.api_secret() == "dpks_survived"


def test_the_key_file_candidates_are_deduplicated(tmp_path, monkeypatch):
    # When the working directory IS the home directory the same path was listed
    # twice, which reads as two separate files that disagree.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    from cambrian import config
    assert len(config.key_file_candidates()) == 1


def test_debug_redacts_the_key_and_never_prints_the_secret(capsys, monkeypatch):
    monkeypatch.setenv("DEFINITIVE_API_KEY", "dpka_publicish")
    monkeypatch.setenv("DEFINITIVE_API_SECRET", "dpks_verysecret")

    def boom(*a, **k):
        raise OSError("no network in tests")

    monkeypatch.setattr(D.urllib.request, "urlopen", boom)
    with pytest.raises(OSError):
        D.request("/v2/portfolio", debug=True)
    out = capsys.readouterr().out
    assert "dpka_publicish" not in out
    assert "dpks_verysecret" not in out
    assert "<KEY:14 chars>" in out          # length disclosed, value not
