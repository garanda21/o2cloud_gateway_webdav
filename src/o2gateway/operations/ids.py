from __future__ import annotations

import secrets
import time


def new_operation_id() -> str:
    return "op_%x_%s" % (int(time.time() * 1000), secrets.token_hex(5))

