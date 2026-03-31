import time
import requests
from queue import Queue
from threading import Thread

LOG_FILE = "app.log"
BACKEND_URL = "http://127.0.0.1:8000/logs"

log_queue = Queue()


def follow(file):
    file.seek(0, 2)
    while True:
        line = file.readline()
        if not line:
            time.sleep(0.1)
            continue
        yield line


def parse_log(line):
    try:
        level, rest = line.strip().split(": ", 1)

        if rest.startswith("["):
            service, message = rest.split("] ", 1)
            service = service.strip("[]")
        else:
            service = "unknown"
            message = rest

    except:
        level = "UNKNOWN"
        service = "unknown"
        message = line.strip()

    return {
        "timestamp": time.time(),
        "level": level,
        "service": service,
        "message": message
    }


def producer():
    with open(LOG_FILE, "r") as f:
        for line in follow(f):
            parsed = parse_log(line)
            log_queue.put(parsed)


def consumer():
    while True:
        log = log_queue.get()

        while True:
            try:
                res = requests.post(BACKEND_URL, json=log, timeout=2)
                if res.status_code == 200:
                    break
            except:
                print("Retrying backend...")
                time.sleep(2)

        log_queue.task_done()


if __name__ == "__main__":
    Thread(target=producer, daemon=True).start()
    Thread(target=consumer, daemon=True).start()

    while True:
        time.sleep(1)