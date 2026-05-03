import subprocess
import requests
import time
import json
import select 
import random

BACKEND_URL = "http://127.0.0.1:8000/logs"


# -----------------------------
# Get target pods
# -----------------------------
def get_target_pods():
    try:
        out = subprocess.check_output(
            [
                "kubectl",
                "get",
                "pods",
                "-o",
                "jsonpath={.items[*].metadata.name}"
            ]
        ).decode()

        return out.split()

    except:
        return []

# -----------------------------
# Stream logs from multiple pods
# -----------------------------
import select

def stream_logs():

    pods = get_target_pods()

    processes = []

    for pod in pods:
        try:
            p = subprocess.Popen(
                ["kubectl", "logs", "-f", pod],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            processes.append((pod, p))
        except:
            continue

    # collect all stdout pipes
    pipes = [p.stdout for _, p in processes]

    while True:

        ready, _, _ = select.select(pipes, [], [], 1)

        for pipe in ready:

            line = pipe.readline()

            if not line:
                continue

            # find which pod this pipe belongs to
            for pod, proc in processes:
                if proc.stdout == pipe:
                    yield f"[pod/{pod}] {line.strip()}"
                    break
# -----------------------------
# Parse structured logs
# -----------------------------

def parse_log(line, service):
    try:
        data = json.loads(line)

        severity = data.get("severity", "info").upper()

        if severity in ["ERROR", "CRITICAL"]:
            level = "ERROR"

        elif severity in ["WARNING", "WARN"]:
            level = "WARNING"

        else:
            level = "INFO"

        return {
            "timestamp": time.time(),
            "level": level,
            "service": service,
            "message": data.get("message", line)
        }

    except:
        return {
            "timestamp": time.time(),
            "level": "INFO",
            "service": service,
            "message": line
        }
# -----------------------------
# Send to backend
# -----------------------------

def send_log(log):
    try:
        requests.post(BACKEND_URL, json=log)
    except Exception as e:
        print("Failed to send log:", e)


# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    print("Starting Kubernetes multi-pod log collector...")

    last_emit_time = 0   # 🔥 rate limiter

    for raw_line in stream_logs():

        if not raw_line:
            continue

        if raw_line.startswith("[pod/"):

            prefix_end = raw_line.find("]")

            if prefix_end == -1:
                continue

            pod_name = raw_line[5:prefix_end]
            log_line = raw_line[prefix_end+1:].strip()

            # ✅ clean service name
            service = pod_name.split("-")[0]

            # 🚫 ignore noisy service (optional but recommended)
            if service == "loadgenerator":
                continue

            log = parse_log(log_line, service)

            # 🔴 drop INFO logs
            if log["level"] == "INFO":
                # keep 1 in 20 INFO logs
                if random.random() > 0.1:
                    continue

            # 🔥 rate limiting (important)
            now = time.time()
            if now - last_emit_time < 0.1:   # max ~10 logs/sec
                continue
            last_emit_time = now

            print(log)

            send_log(log)