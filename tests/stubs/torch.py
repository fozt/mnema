"""An external torch stub to check the streaming worker's thread budget without the library."""

import os
from types import SimpleNamespace

_threads = 1
omp_at_import = os.environ.get("OMP_NUM_THREADS")
cuda = SimpleNamespace(is_available=lambda: False)


def get_num_threads():
    return _threads


def set_num_threads(value):
    global _threads
    _threads = value
