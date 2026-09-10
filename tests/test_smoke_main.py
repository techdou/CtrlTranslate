"""打包入口冒烟：main.py 顶层 import 链必须可导入。

盲区教训（v1.1.0 exe 启动失败）：单测从不 import main.py，Qt 名字放错模块
（QUrl 误入 QtGui）这类入口级 ImportError 测试全绿、只有打包后 exe 才炸。
"""

def test_main_module_importable():
    import main  # noqa: F401


def test_ctrlapp_is_qobject():
    """热键/取词/更新的信号从工作线程 emit，接收者 CtrlApp 必须是 QObject：
    Qt 只对带线程亲和（QObject）的接收者排队回主线程，否则直连——OCR 热键
    回调曾在 keyboard 钩子线程里直接创建遮罩窗口（未定义行为）。"""
    from PySide6.QtCore import QObject

    import main

    assert issubclass(main.CtrlApp, QObject)
