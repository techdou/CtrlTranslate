"""打包入口冒烟：main.py 顶层 import 链必须可导入。

盲区教训（v1.1.0 exe 启动失败）：单测从不 import main.py，Qt 名字放错模块
（QUrl 误入 QtGui）这类入口级 ImportError 测试全绿、只有打包后 exe 才炸。
"""

def test_main_module_importable():
    import main  # noqa: F401
