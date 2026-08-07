from cambrian.runners.llm import judge_messages, parse_verdict


def test_judge_messages_carries_facts():
    msgs = judge_messages({"pad": "bankr", "net_flow_usd": 4000, "liquidity_usd": 30000})
    assert msgs[0]["role"] == "system" and "JSON" in msgs[0]["content"]
    u = msgs[1]["content"]
    assert "pad: bankr" in u and "net_flow_usd: 4000" in u


def test_parse_buy():
    v = parse_verdict('{"decision":"buy","confidence":0.8,"reason":"strong inflow"}')
    assert v["decision"] == "buy" and v["confidence"] == 0.8 and "inflow" in v["reason"]


def test_parse_tolerates_prose_and_fences():
    v = parse_verdict('Sure!\n```json\n{"decision":"buy","confidence":0.6,"reason":"ok"}\n```')
    assert v["decision"] == "buy" and v["confidence"] == 0.6


def test_parse_fails_closed_on_garbage():
    assert parse_verdict("lol no json here")["decision"] == "skip"
    assert parse_verdict("")["decision"] == "skip"


def test_parse_non_buy_is_skip_and_clamps_confidence():
    assert parse_verdict('{"decision":"maybe","confidence":5}')["decision"] == "skip"
    v = parse_verdict('{"decision":"buy","confidence":2.0,"reason":"x"}')
    assert v["confidence"] == 1.0            # clamped to [0,1]
