"""One reentrant guard for all Memory OS provider callers in a process.

The environment flag is retained for inherited subprocess compatibility. Within
this process only the calling thread/context is internal: a concurrent user turn
must still capture. The last owner restores exactly the pre-existing env value.
"""
import contextlib
import contextvars
import os
import threading

_lock = threading.RLock()
_depth = 0
_previous = None
_local = contextvars.ContextVar('pz-memory-internal-depth', default=0)


def enter():
    global _depth, _previous
    with _lock:
        if _depth == 0:
            _previous = os.environ.get('PZ_MEMORY_INTERNAL_CALL')
        _depth += 1
        _local.set(_local.get() + 1)
        os.environ['PZ_MEMORY_INTERNAL_CALL'] = '1'


def leave():
    global _depth, _previous
    with _lock:
        if _local.get() <= 0:
            return
        _local.set(_local.get() - 1)
        _depth -= 1
        if _depth == 0:
            if _previous is None:
                os.environ.pop('PZ_MEMORY_INTERNAL_CALL', None)
            else:
                os.environ['PZ_MEMORY_INTERNAL_CALL'] = _previous
            _previous = None


def is_internal():
    with _lock:
        return bool(_local.get()) or (_previous == '1' if _depth else os.environ.get('PZ_MEMORY_INTERNAL_CALL') == '1')


@contextlib.contextmanager
def internal_call():
    enter()
    try:
        yield
    finally:
        leave()
