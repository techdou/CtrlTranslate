from app.core.hotkey import DoubleTapDetector


def fresh(interval=300):
    t = [0.0]
    return t, DoubleTapDetector(interval_ms=interval, clock=lambda: t[0])


def run(det, clock, events):
    """events: [(name, is_down, dt_ms)]"""
    fired = 0
    for name, down, dt in events:
        clock[0] += dt / 1000.0
        if det.feed(name, down):
            fired += 1
    return fired


def test_double_tap_fires():
    t, d = fresh()
    assert run(d, t, [("ctrl", 1, 0), ("ctrl", 0, 50), ("ctrl", 1, 100), ("ctrl", 0, 50)]) == 1


def test_single_tap_no_fire():
    t, d = fresh()
    assert run(d, t, [("ctrl", 1, 0), ("ctrl", 0, 50)]) == 0


def test_slow_double_no_fire():
    t, d = fresh()
    assert run(d, t, [("ctrl", 1, 0), ("ctrl", 0, 50), ("ctrl", 1, 600), ("ctrl", 0, 50)]) == 0


def test_interval_configurable():
    t, d = fresh(interval=600)
    assert run(d, t, [("ctrl", 1, 0), ("ctrl", 0, 50), ("ctrl", 1, 500), ("ctrl", 0, 50)]) == 1


def test_combo_key_dirties_window():
    # Ctrl+C 之后紧接再按 Ctrl：是组合键的一部分，不是双击
    t, d = fresh()
    events = [
        ("ctrl", 1, 0), ("c", 1, 30), ("c", 0, 30), ("ctrl", 0, 30),
        ("ctrl", 1, 50), ("ctrl", 0, 50),
    ]
    assert run(d, t, events) == 0


def test_left_right_ctrl_both_count():
    t, d = fresh()
    assert run(d, t, [("right ctrl", 1, 0), ("right ctrl", 0, 50), ("ctrl", 1, 100), ("ctrl", 0, 50)]) == 1


def test_triple_tap_fires_once():
    t, d = fresh()
    events = [("ctrl", 1, 0), ("ctrl", 0, 50), ("ctrl", 1, 50), ("ctrl", 0, 50),
              ("ctrl", 1, 50), ("ctrl", 0, 50)]
    assert run(d, t, events) == 1


def test_tap_then_pause_then_double():
    # 第一次单击后隔很久，再来一次标准双击：应触发一次
    t, d = fresh()
    events = [("ctrl", 1, 0), ("ctrl", 0, 100), ("ctrl", 1, 800), ("ctrl", 0, 50),
              ("ctrl", 1, 100), ("ctrl", 0, 50)]
    assert run(d, t, events) == 1
