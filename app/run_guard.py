"""当前单进程实例的账号互斥；手动执行和调度执行共用同一把锁。"""

import threading


_registry = {}
_registry_lock = threading.Lock()


def try_acquire(account_id: int):
    """非阻塞获取；返回需由调用者释放的锁，忙时返回 None。"""
    with _registry_lock:
        lock = _registry.setdefault(account_id, threading.Lock())
    if lock.acquire(blocking=False):
        return lock
    return None
