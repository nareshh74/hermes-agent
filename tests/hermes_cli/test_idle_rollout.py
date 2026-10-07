from hermes_cli.idle_rollout import decide, eta_seconds, idle_minutes


def test_idle_decision():
    m = 5
    assert decide(1000, None, None, m) == "notice"  # no sessions
    assert decide(1000, 1000 - 299, None, m) == "wait"  # recent prompt
    assert decide(1000, 1000 - 300, None, m) == "notice"  # idle N minutes
    assert decide(1000, 900, 950, m) == "wait"  # notice sent, window running
    assert decide(1300, 900, 1000, m) == "update"  # window elapsed, no prompt
    assert decide(1300, 1001, 1000, m) == "postpone"  # prompt after notice


def test_config_and_eta():
    assert idle_minutes({}) == 5
    assert idle_minutes({"updates": {"idle_minutes": 2}}) == 2
    assert idle_minutes({"updates": {"idle_minutes": "x"}}) == 5
    assert eta_seconds([]) == 300
    assert eta_seconds([100, 200, "bad"]) == 150
