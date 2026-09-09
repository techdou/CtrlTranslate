"""单实例锁：QLocalServer 命名管道（listen 即原子锁，第二实例通知主实例后退出）。

替代此前 main.py 里的 QLockFile 方案：LockFile 只能拦下第二实例弹"已在运行"
提示，把找托盘图标的事甩给用户；本方案第二实例直接唤醒主实例弹设置窗，
双击 exe 的"打开程序"意图被完整满足。

键名带用户名隔离：多用户同时登录（快速切换用户）时互不误判。
Windows named pipe 随进程销毁无残留文件；listen 失败重试一次清理，
仍失败则宽容放行（单实例是增强不是门槛，宁可双开不能不开）。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger("ctrltrans.singleton")


def instance_key(app_name: str = "CtrlTranslate") -> str:
    """键名 = 应用名 + Windows 用户名。纯函数便于单测。

    用 USERNAME 环境变量而非 os.getlogin()：后者需要控制终端，
    服务/CI 环境下会抛 OSError。"""
    user = os.environ.get("USERNAME") or "user"
    return f"{app_name.lower()}-{user}"


class SingleInstance(QObject):
    """acquire() 抢主实例锁；失败方 notify_primary() 唤醒主实例后退出。

    主实例通过 activated 信号得知唤醒请求（如弹出设置窗）。
    """

    activated = Signal()

    def __init__(self, app_name: str = "CtrlTranslate", parent: QObject | None = None):
        super().__init__(parent)
        self._key = instance_key(app_name)
        self._server: QLocalServer | None = None
        self._notify_sock: QLocalSocket | None = None  # 唤醒 socket 引用，防 GC 抢跑

    @property
    def is_primary(self) -> bool:
        return self._server is not None

    def acquire(self) -> bool:
        """尝试成为主实例。True = 本实例是主实例（或锁服务异常的宽容放行）。"""
        probe = QLocalSocket()
        probe.connectToServer(self._key)
        if probe.waitForConnected(300):  # 已有实例在监听
            probe.disconnectFromServer()
            return False
        server = QLocalServer(self)
        if not server.listen(self._key):
            # 竞态：probe 与 listen 之间刚好有实例建好 server；重探一次
            probe2 = QLocalSocket()
            probe2.connectToServer(self._key)
            if probe2.waitForConnected(300):
                probe2.disconnectFromServer()
                return False
            logger.error("single-instance listen failed: %s", server.errorString())
            return True  # 宽容放行：锁不住不拦启动
        self._server = server
        self._server.newConnection.connect(self._on_new_connection)
        return True

    def notify_primary(self) -> None:
        """第二实例退出前唤醒主实例（发 activate 指令）。

        sock 必须被持有到主实例读走数据：局部变量会被 GC 立即析构管道句柄，
        Windows 下服务端尚未读取的缓冲数据随之丢弃（踩过：信号正常到达
        但 readAll 恒空）。断开后再延迟释放。"""
        sock = QLocalSocket()
        sock.connectToServer(self._key)
        if sock.waitForConnected(1000):
            sock.write(b"activate\n")
            sock.waitForBytesWritten(1000)
            sock.flush()
            sock.disconnectFromServer()
            if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                sock.waitForDisconnected(1000)
            self._notify_sock = sock  # 持有防 GC 提前销毁句柄
        else:
            logger.warning("notify_primary failed: %s", sock.errorString())
            self._notify_sock = sock  # 失败路径同样持有到析构安全时点

    def release(self) -> None:
        """显式释放（进程退出时管道自动销毁，此调用是仪式性清理）。"""
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None

    # ---------------------------------------------------------------- 内部

    def _on_new_connection(self) -> None:
        # 一次 newConnection 信号把队列里全部连接消费完——探测连接（acquire 的
        # probe 也会触发本信号）会抢先消费掉唯一一次信号，带数据的连接就留在
        # pending 队列里永远没人取了
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            if conn is None:
                break
            # 客户端 write 完立即断开（notify_primary 的常态），等我们挂上
            # readyRead 槽时数据常已进缓冲甚至连接已断——短同步等待收全数据
            conn.waitForReadyRead(300)
            data = bytes(conn.readAll())
            if b"activate" in data:
                self.activated.emit()
            conn.disconnectFromServer()
            conn.deleteLater()
