import threading


class Memo:
    def __init__(self):
        self.values = {}
        self._lock = threading.Lock()
        self._in_progress = {}  # key -> threading.Event

    def get(self, key, factory):
        while True:
            with self._lock:
                if key in self.values:
                    return self.values[key]
                event = self._in_progress.get(key)
                if event is None:
                    event = threading.Event()
                    self._in_progress[key] = event
                    break
            # Another thread is computing this key: wait for it.
            event.wait()

        # We own the computation for this key.
        try:
            value = factory()
        except BaseException:
            with self._lock:
                del self._in_progress[key]
                event.set()
            raise
        with self._lock:
            self.values[key] = value
            del self._in_progress[key]
            event.set()
        return value
