"""QQ 通道：OneBot11 WebSocket（主动发 + 被动收）。

- 发送：短连接 + 锁保护，断开自动重连重试（修 WinError 10053 类死连接问题）
- 接收：常驻 daemon 线程维持长连接，把私聊消息回调给上层（角色的"被动入口"）
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable

import websocket

from core.logger import get_logger

log = get_logger("channel.qq")

# 回显标记
_ECHO = "oquilla"


class OneBot11Client:
    def __init__(
        self,
        ws_url: str,
        targets: dict[str, list[str]] | None = None,
        access_token: str | None = None,
    ):
        self.ws_url = ws_url
        self.targets = targets or {"private": []}
        self.access_token = access_token
        self._send_lock = threading.Lock()
        self._ws: websocket.WebSocket | None = None
        self._listen_thread: threading.Thread | None = None
        self._listen_stop = threading.Event()
        self.inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self.on_message: Callable[[dict[str, Any]], None] | None = None

    # ---------- 底层连接 ----------
    def _open(self, timeout: int = 10) -> websocket.WebSocket:
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
        return websocket.create_connection(self.ws_url, timeout=timeout, header=headers)

    # ---------- 心跳（SnowLuma 约 90s 无活动会踢半开连接，需主动 ping 保活）----------
    def _start_heartbeat(self, ws: websocket.WebSocket, interval: int = 25) -> threading.Thread | None:
        """对一条 WS 连接启动守护心跳线程，定时发 Ping，防被服务端判定半开而断开。"""
        if ws is None:
            return None
        try:
            ws.ping("")
        except Exception:  # noqa: BLE001
            pass

        def _beat() -> None:
            while not self._listen_stop.is_set():
                time.sleep(interval)
                if self._listen_stop.is_set():
                    break
                try:
                    ws.ping("")
                except Exception:  # noqa: BLE001
                    # 发送连接断掉时由上层重连；接收连接断掉靠 _listen_loop 重连兜底
                    return

        t = threading.Thread(target=_beat, daemon=True, name="qq-heartbeat")
        t.start()
        return t

    def connect(self) -> None:
        """建立发送用短连接。"""
        self.close()
        self._ws = self._open()
        self._start_heartbeat(self._ws)
        log.info("OneBot ws connected: %s (auth=%s)", self.ws_url, bool(self.access_token))

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._ws = None

    def _send_ws(self, payload: dict[str, Any], timeout: int) -> dict[str, Any] | None:
        """在发送连接上发一条并等回执；断开自动重连重试一次。"""
        if not self._ws:
            self.connect()
        echo = payload.get("echo", _ECHO)
        payload["echo"] = echo
        try:
            self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            log.warning("send 失败(%s)，尝试重连一次", e)
            try:
                self.connect()
                self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as e2:  # noqa: BLE001
                log.error("重连后仍发送失败: %s", e2)
                return None
        # 读回执：跳过非本 echo 的消息
        try:
            self._ws.settimeout(timeout)
            while True:
                raw = self._ws.recv()
                try:
                    resp = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if resp.get("echo") == echo:
                    return resp
                # 非目标回执继续等
        except Exception as e:  # noqa: BLE001
            log.warning("回执读取异常(%s)", e)
            return None

    # ---------- 发送动作 ----------
    def send_private(self, user_id: str, message: str) -> dict[str, Any] | None:
        with self._send_lock:
            resp = self._send_ws(
                {"action": "send_private_msg", "params": {"user_id": int(user_id), "message": message}, "echo": "send"},
                8,
            )
            if resp:
                log.info("send_private -> %s status=%s retcode=%s", user_id, resp.get("status"), resp.get("retcode"))
            return resp

    def send_group(self, group_id: str, message: str) -> None:
        with self._send_lock:
            self._send_ws({"action": "send_group_msg", "params": {"group_id": int(group_id), "message": message}}, 8)
            log.info("sent to group %s: %s", group_id, message)

    def send_private_image(self, user_id: str, image_path: str, text: str = "") -> dict[str, Any] | None:
        path = str(image_path).replace("\\", "/")
        cq = f"[CQ:image,file={path}]"
        message = f"{text}\n{cq}" if text else cq
        with self._send_lock:
            resp = self._send_ws(
                {"action": "send_private_msg", "params": {"user_id": int(user_id), "message": message}, "echo": "sendimg"},
                15,
            )
            if resp:
                log.info("send_private_image -> %s status=%s retcode=%s", user_id, resp.get("status"), resp.get("retcode"))
            return resp

    def broadcast_private(self, message: str, media: list[str] | None = None) -> None:
        for uid in self.targets.get("private", []):
            try:
                if media:
                    for idx, m in enumerate(media):
                        # 第一张带配文，其余纯图（用 enumerate 而非 index，避免重复路径配错文字）
                        self.send_private_image(uid, m, text=message if idx == 0 else "")
                else:
                    self.send_private(uid, message)
            except Exception as e:  # noqa: BLE001
                log.error("send to %s failed: %s", uid, e)

    # ---------- 被动接收 ----------
    def start_listen(self, on_message: Callable[[dict[str, Any]], None]) -> None:
        """启动常驻接收线程，把私聊事件回调给上层。幂等。"""
        self.on_message = on_message
        if self._listen_thread and self._listen_thread.is_alive():
            return
        self._listen_stop.clear()
        self._listen_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="qq-listen"
        )
        self._listen_thread.start()
        log.info("QQ 接收线程已启动")

    def stop_listen(self) -> None:
        self._listen_stop.set()

    def _listen_loop(self) -> None:
        while not self._listen_stop.is_set():
            ws = None
            try:
                ws = self._open()
                ws.settimeout(60)
                self._start_heartbeat(ws)  # 保活：防被 SnowLuma 90s 无活动判定半开而踢
                log.info("QQ 监听连接建立")
                while not self._listen_stop.is_set():
                    raw = ws.recv()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    # 只关注私聊消息事件
                    if data.get("post_type") == "message" and data.get("message_type") == "private":
                        try:
                            if self.on_message:
                                self.on_message(data)
                        except Exception as e:  # noqa: BLE001
                            log.error("处理接收消息回调异常: %s", e)
            except Exception as e:  # noqa: BLE001
                if self._listen_stop.is_set():
                    break
                log.warning("监听连接断开(%s)，3 秒后重连", e)
            finally:
                if ws:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001
                        pass
            if not self._listen_stop.is_set():
                time.sleep(3)