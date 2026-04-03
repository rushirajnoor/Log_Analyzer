import subprocess
import requests
import time

BACKEND_URL = "http://127.0.0.1:8000/logs"


def stream_logs():
    # 🔥 ONLY recent logs + follow live
    process = subprocess.Popen(
        ["docker", "logs", "-f", "--since=5s", "fastapi-app"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    for line in process.stdout:
        yield line.strip()


def parse_log(line):
    if "ERROR" in line:
        level = "ERROR"
    elif "WARNING" in line:
        level = "WARNING"
    else:
        level = "INFO"

    return {
        "timestamp": time.time(),
        "level": level,
        "service": "fastapi-app",
        "message": line
    }


def send_log(log):
    try:
        requests.post(BACKEND_URL, json=log)
    except Exception as e:
        print("Failed to send log:", e)


if __name__ == "__main__":
    print("Starting log collector...")

    for log_line in stream_logs():
        if not log_line:
            continue  # skip empty lines

        log = parse_log(log_line)
        print(log)
        send_log(log)