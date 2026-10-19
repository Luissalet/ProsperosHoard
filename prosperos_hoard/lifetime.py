"""Owned notification threads; close only after producers have stopped."""
import threading


class Threads:
    def __init__(self):
        self.lock = threading.Lock()
        self.threads = []
        self.closed = False

    def start(self, target, args, name):
        with self.lock:
            if self.closed:
                return
            self.threads = [t for t in self.threads if t.is_alive()]
            thread = threading.Thread(target=target, args=args, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        with self.lock:
            self.closed = True
            threads = list(self.threads)
        for thread in threads:
            thread.join()
        self.threads.clear()
