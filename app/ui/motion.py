"""动效执行器：全部动画走 animate() 收口，时长/曲线从 theme.MOTION 取令牌。

QSS 不支持 transition，hover 瞬变是原生语言不动它；这里的动画只服务
窗口/控件的出现、消失、生长、呼吸、错误抖动这类"状态迁移"。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QVariantAnimation


def animate(setter: Callable[[float], None], start: float, end: float,
            dur_ms: int, ease: str = "OutCubic",
            done: Callable[[], None] | None = None,
            loop: int = 1) -> QVariantAnimation:
    """把 start→end 的插值逐帧喂给 setter。

    返回动画句柄，调用方持有（便于中断/重启）；不持有且无 parent 时
    动画可能被 GC 提前终止——统一由调用方赋给 self._xxx_anim。
    loop=-1 表示无限循环（loading 呼吸用），记得显式 stop()。
    """
    va = QVariantAnimation()
    va.setStartValue(start)
    va.setEndValue(end)
    va.setDuration(dur_ms)
    va.setEasingCurve(getattr(QEasingCurve.Type, ease))
    va.setLoopCount(loop)
    va.valueChanged.connect(lambda v: setter(float(v)))
    if done is not None:
        va.finished.connect(done)
    va.start()
    return va


def breathe(setter: Callable[[float], None], period_ms: int,
            low: float, high: float) -> QVariantAnimation:
    """一个完整正弦呼吸周期：0→峰值→0，无 loop 边界跳变。

    v 匀速走 [0,1]，经 sin(v·π) 映射成"缓升到峰值再缓落"的完整起伏，
    loop=-1 无限循环时首尾值相同（都是 low），视觉连续。
    """
    import math

    va = QVariantAnimation()
    va.setStartValue(0.0)
    va.setEndValue(1.0)
    va.setDuration(period_ms)
    va.setLoopCount(-1)
    va.valueChanged.connect(lambda v: setter(low + (high - low) * math.sin(float(v) * math.pi)))
    va.start()
    return va
