import subprocess
import time

from rca_engine import run_rca, get_latest_timestamp

print("RUNNING FILE:", __file__)

# -----------------------------
# Docker helpers
# -----------------------------

def get_all_containers():
    try:
        out = subprocess.check_output(
            ["docker", "ps", "-a", "--format", "{{.Names}}"]
        ).decode().splitlines()
        return out
    except:
        return []


def is_running(container):
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        return out == "true"
    except:
        return False


def restart(container):
    print(f"Restarting {container}")
    subprocess.run(["docker", "restart", container])


# -----------------------------
# Auto-discovery filter
# -----------------------------

def filter_project_containers(containers):
    keywords = ["redis", "fastapi", "postgres"]

    filtered = []
    for c in containers:
        for k in keywords:
            if k in c:
                filtered.append(c)
                break

    return filtered


def check_and_fix_containers():
    containers = get_all_containers()
    containers = filter_project_containers(containers)

    for c in containers:
        if not is_running(c):
            print(f"\n--- HEALTH CHECK ---")
            print(f"{c} is DOWN → restarting")

            restart(c)
            time.sleep(3)


# -----------------------------
# RCA-based fallback
# -----------------------------

def fix_from_cause(cause):
    c = (cause or "").lower()

    if "redis" in c:
        return "redis"

    if "fastapi" in c:
        return "fastapi-app"

    if "postgres" in c or "database" in c:
        return "postgres"

    return None


# -----------------------------
# MAIN LOOP
# -----------------------------

def main():
    print("Starting Auto-Remediation (Auto-Discovery Mode)...")

    while True:
        try:
            # 🔥 1. Health check layer (works even without logs)
            check_and_fix_containers()

            # 🔥 2. RCA layer (log-based)
            ts = get_latest_timestamp()

            if ts is None:
                print("No logs yet")
                time.sleep(5)
                continue

            result = run_rca(ts)

            cause = result.get("inferred_cause", "Unknown")

            print("\n--- RCA RESULT ---")
            print("Cause:", cause)

            service = fix_from_cause(cause)

            if not service:
                print("No action needed")
                time.sleep(10)
                continue

            running = is_running(service)
            print(f"{service} running:", running)

            print(f"{service} → restarting (safe recovery)")
            restart(service)

            print("\nWaiting...\n")
            time.sleep(10)

        except Exception as e:
            print("Error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()