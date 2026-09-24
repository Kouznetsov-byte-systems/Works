import queue
import sys
import threading
import time
from typing import Callable, List, Optional

class ThreadSafeLogInterceptor:
    def __init__(self, max_buffer: int = 500) -> None:
        self._log_q = queue.Queue(maxsize=max_buffer)
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[str], None]] = []

    def register_gui_callback(self, cb: Callable[[str], None]) -> None:
        if not cb:
            raise ValueError("Callback missing.")
        with self._lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)

    def push_log_frame(self, level: str, msg: str) -> None:
        if not level or not msg:
            return
        
        frame = f"[{level}] {int(time.time())} :: {msg.strip()}"
        try:
            self._log_q.put_nowait(frame)
            self._dispatch_frame(frame)
        except queue.Full:
            pass

    def _dispatch_frame(self, frame: str) -> None:
        with self._lock:
            for cb in self._callbacks:
                try:
                    cb(frame)
                except Exception:
                    pass

    def drain_buffer(self) -> List[str]:
        batch = []
        with self._lock:
            while not self._log_q.empty():
                try:
                    batch.append(self._log_q.get_nowait())
                except queue.Empty:
                    break
        return batch

if __name__ == "__main__":
    interceptor = ThreadSafeLogInterceptor()
    def dummy_handler(m: str) -> None:
        pass
    interceptor.register_gui_callback(dummy_handler)
    interceptor.push_log_frame("INFO", "ThreadSafe GUI Log Interceptor initialized nominally.")
