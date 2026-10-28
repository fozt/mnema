"""Date formatting of the server area: invitation expiry."""

from __future__ import annotations

import time

from .fmt import month_short


def invite_until(timestamp: float) -> str:
    """When an invitation expires, local time: ru «28.10 14:30», en «Oct 28, 14:30»."""
    from . import current

    moment = time.localtime(timestamp)
    if current() == "ru":
        return time.strftime("%d.%m %H:%M", moment)
    return f"{month_short(moment.tm_mon)} {moment.tm_mday}, {time.strftime('%H:%M', moment)}"
