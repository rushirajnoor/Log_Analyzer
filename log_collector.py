import subprocess
import requests
import time
import json

BACKEND_URL = "http://127.0.0.1:8000/logs"


# -----------------------------
# Get target pods
# -----------------------------

def get_target_pods():
    out = subprocess.check_output(
        [
            "kubectl",
            "get",
            "pods",
            "-o",
            "jsonpath={.items[*].metadata.name}"
        ]
    ).decode()

    all_pods = out.split()

    targets = []

    for p in all_pods:
        if (
            "frontend" in p or
            "cartservice" in p or
            "redis-cart" in p
        ):
            targets.append(p)

    return targets


# -----------------------------
# Stream logs from multiple pods
# -----------------------------

def stream_logs():
    process = subprocess.Popen(
        [
            "kubectl",
            "logs",
            "-f",
            "deployment/frontend"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    for line in process.stdout:
        if line:
            yield "[pod/frontend] " + line.strip()


# -----------------------------
# Parse structured logs
# -----------------------------

def parse_log(line):
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
            "service": "unknown",
            "message": data.get("message", line)
        }

    except:
        # fallback for non-JSON logs
        return {
            "timestamp": time.time(),
            "level": "INFO",
            "service": "unknown",
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

    for raw_line in stream_logs():

        if not raw_line:
            continue

        # Example:
        # [pod/frontend-xxxxx] {"message":"..."}
        if raw_line.startswith("[pod/"):

            prefix_end = raw_line.find("]")

            if prefix_end == -1:
                continue

            pod_name = raw_line[5:prefix_end]
            log_line = raw_line[prefix_end+1:].strip()

            log = parse_log(log_line)

            # 🔥 Correct pod attribution
            log["service"] = pod_name

            print(log)

            send_log(log)