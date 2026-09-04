from app.core.hotkey import DoubleTapDetector


def fresh(interval=300, key="ctrl"):
    t = [0.0]
    return t, DoubleTapDetector(target_key=key, interval_ms=interval, clock=lambda: t[0])


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


# ---------------------------------------------------------------- 触发键可选

def test_normalize_variants():
    n = DoubleTapDetector.normalize
    assert n("Ctrl") == "ctrl"
    assert n("left ctrl") == "ctrl"
    assert n("right control") == "ctrl"
    assert n("alt") == "alt"
    assert n("left alt") == "alt"
    assert n("right alt") == "alt"
    assert n("shift") == "shift"
    assert n("left shift") == "shift"
    assert n("right shift") == "shift"
    assert n("c") == "c"
    assert n("") == ""


def test_double_alt_fires():
    # 左右 Alt 混用也算同一键（与 Ctrl 行为一致）
    t, d = fresh(key="alt")
    events = [("left alt", 1, 0), ("left alt", 0, 50), ("right alt", 1, 100), ("alt", 0, 50)]
    assert run(d, t, events) == 1


def test_double_shift_fires():
    t, d = fresh(key="shift")
    events = [("shift", 1, 0), ("shift", 0, 50), ("left shift", 1, 100), ("shift", 0, 50)]
    assert run(d, t, events) == 1


def test_non_target_key_double_ignored():
    # 目标是 Ctrl 时，双击 Alt 不触发（反之亦然）
    t, d = fresh()
    events = [("alt", 1, 0), ("alt", 0, 50), ("alt", 1, 100), ("alt", 0, 50)]
    assert run(d, t, events) == 0
    t2, d2 = fresh(key="alt")
    events2 = [("ctrl", 1, 0), ("ctrl", 0, 50), ("ctrl", 1, 100), ("ctrl", 0, 50)]
    assert run(d2, t2, events2) == 0


def test_service_set_key_switches_detector():
    from app.core.hotkey import HotkeyService

    svc = HotkeyService(interval_ms=450, key="ctrl")
    assert svc.detector.target_key == "ctrl"
    svc.set_key("alt")
    assert svc.detector.target_key == "alt"
    assert svc.detector.interval_ms == 450  # 切键不丢间隔配置
    svc.set_key("alt")  # 幂等：重复设置不重建
    assert svc.detector.target_key == "alt"
