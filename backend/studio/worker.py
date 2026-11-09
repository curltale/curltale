"""A separate worker for the Library: `python -m studio.worker`. Runs the same job queue as the web process, so heavy document work can live
in its own container (set SERVICE_ROLE=api and LIBRARY_WORKERS=0 on the web one, or just leave the defaults to run everything in one)."""
import signal
import threading
import time
from . import db
from .library import jobs


def main():
    for _ in range(60):                                                 # the database may still be starting
        try:
            db.init_schema()
            break
        except Exception as e:
            print('waiting for the database:', e, flush=True)
            time.sleep(2)
    import os
    from . import settings
    n = max(1, settings.get('LIBRARY_WORKERS') or int(os.environ.get('WORKER_THREADS', '2')))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    from .library import pipeline  # noqa: F401
    threads = [threading.Thread(target=jobs.work_loop, args=(f'worker-{i}', stop), daemon=True) for i in range(n)]
    [t.start() for t in threads]
    print(f'library worker running with {n} threads', flush=True)
    stop.wait()
    [t.join(timeout=10) for t in threads]


if __name__ == '__main__':
    main()
