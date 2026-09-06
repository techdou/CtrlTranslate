"""更新检查纯函数单测：语义化版本比较。"""

from app.core.update import is_newer


def test_is_newer_basic():
    assert is_newer("1.2.0", "1.1.0")
    assert not is_newer("1.1.0", "1.2.0")
    assert not is_newer("1.1.0", "1.1.0")


def test_is_newer_handles_v_prefix_and_padding():
    assert is_newer("v1.2", "1.1.0")        # v 前缀 + 不等长补零
    assert not is_newer("1.1", "1.1.0")     # 1.1 == 1.1.0
    assert is_newer("2.0", "1.9.9")


def test_is_newer_garbage_input():
    assert not is_newer("", "1.0.0")
    assert not is_newer("unknown", "1.0.0")  # 解析不出的部分按 0 截断
