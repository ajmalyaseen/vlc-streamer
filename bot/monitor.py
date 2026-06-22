"""In-memory registry of who is currently streaming what.

Lightweight and side-effect-free: the HTTP layer calls `touch()` on each
incoming /stream request; it never influences streaming behaviour. `current()`
returns the entries seen within the TTL window (a viewer is "active" while VLC
keeps making range requests). Purely for the /streamusers admin view.
"""
import time


class StreamMonitor:
    def __init__(self, ttl: int = 45) -> None:
        self.ttl = ttl
        self._active = {}  # (uid, chat_id, msg_id) -> {uid, file_name, last_seen}

    def touch(self, uid, chat_id, msg_id, file_name) -> None:
        self._active[(uid, chat_id, msg_id)] = {
            "uid": uid,
            "chat_id": chat_id,
            "msg_id": msg_id,
            "file_name": file_name,
            "last_seen": time.monotonic(),
        }

    def current(self) -> list:
        now = time.monotonic()
        # prune + return fresh entries
        fresh = []
        for key in list(self._active.keys()):
            entry = self._active[key]
            if now - entry["last_seen"] > self.ttl:
                del self._active[key]
            else:
                fresh.append(entry)
        return fresh
