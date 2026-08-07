from cambrian.runners.agents import (agent_farm, agent_flow, agent_momentum,
                                     agent_safety, agent_smart_money,
                                     agent_sniper, confluence)


def test_flow_rewards_accumulation_and_zeros_distribution():
    assert agent_flow(5000, 20000)[0] == 1.0
    assert agent_flow(-100, 20000)[0] == 0.0        # net out
    assert agent_flow(4000, 0)[0] == 0.0            # no volume


def test_sniper_and_farm_lower_is_better():
    assert agent_sniper(0.0)[0] > agent_sniper(0.9)[0]
    assert agent_farm(2)[0] > agent_farm(60)[0]
    assert agent_farm(60)[0] == 0.0


def test_safety_gate_values():
    assert agent_safety(True, None)[0] == 1.0       # verified pad
    assert agent_safety(False, True)[0] == 0.8      # sim sellable
    assert agent_safety(False, False)[0] == 0.0     # honeypot
    assert 0 < agent_safety(False, None)[0] < 0.8   # unverified/unchecked


def test_smart_money_and_momentum():
    assert agent_smart_money(3)[0] == 1.0
    assert agent_smart_money(0)[0] == 0.0
    assert agent_momentum(8, 2)[0] > agent_momentum(2, 8)[0]


def test_confluence_needs_agreement_and_passes_safety():
    strong = {"flow": 0.9, "smart_money": 0.8, "sniper": 1.0, "momentum": 0.7, "farm": 0.9}
    c = confluence(strong, safety=1.0)
    assert c["tier"] == "strong" and c["agree"] >= 3 and not c["gated"]


def test_confluence_honeypot_is_blocked_regardless():
    strong = {"flow": 1.0, "smart_money": 1.0, "sniper": 1.0, "momentum": 1.0, "farm": 1.0}
    c = confluence(strong, safety=0.0)                # honeypot
    assert c["gated"] and c["tier"] == "blocked"


def test_confluence_one_signal_is_not_enough():
    lone = {"flow": 1.0, "smart_money": 0.0, "sniper": 0.0, "momentum": 0.0, "farm": 0.0}
    c = confluence(lone, safety=1.0)
    assert c["tier"] in ("weak", "watch") and c["agree"] == 1
