"""运行中止注册表（移植自原版 Aniu business/runs/abort.py + abort_registry.py 的思路）。

为每个进行中的运行维护一个 threading.Event；API 的 abort 端点置位事件，
运行线程在 LLM 迭代、工具调用、执行动作等检查点感知并停止。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class RunAbortRegistry:
    def __init__(self) -> None:
        self._events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()

    def register(self, run_id: int) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._events[run_id] = event
        return event

    def unregister(self, run_id: int) -> None:
        with self._lock:
            self._events.pop(run_id, None)

    def abort(self, run_id: int) -> bool:
        """置位中止事件。返回是否存在该运行的活动事件。"""
        with self._lock:
            event = self._events.get(run_id)
        if event is None:
            return False
        event.set()
        logger.info("run abort requested: run_id=%s", run_id)
        return True

    def is_registered(self, run_id: int) -> bool:
        with self._lock:
            return run_id in self._events


run_abort_registry = RunAbortRegistry()
