import subprocess
import time

from rca_engine import run_rca, get_latest_timestamp, get_error_count

COOLDOWN = 30
last_action_time = 0
last_action_signature = None

# 🔒 Only allow safe commands
ALLOWED_COMMANDS = {"docker"}


# -----------------------------
# Docker Helpers
# -----------------------------

def get_all_containers():
    try:
        result = subprocess.check_output(
            ["docker", "ps", "-a", "--format", "{{.Names}}"]
        ).decode().splitlines()
        return result
    except:
        return []


def is_container_running(name):
    try:
        result = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        return result == "true"
    except:
        return False


def filter_containers(containers):
    # Only monitor your project services
    keywords = ["redis", "fastapi", "postgres"]

    filtered = []
    for c in containers:
        for k in keywords:
            if k in c:
                filtered.append(c)
                break

    return filtered


# -----------------------------
# Execution
# -----------------------------

def execute_step(step):
    parts = step.split()

    if not parts:
        return

    if parts[0] not in ALLOWED_COMMANDS:
        print("Blocked unsafe step:", step)
        return

    print("Executing:", parts)
    subprocess.run(parts)


def evaluate_fix(before):
    time.sleep(10)
    after1 = get_error_count()

    time.sleep(5)
    after2 = get_error_count()

    after = min(after1, after2)

    print("Errors after:", after)

    if after < before:
        return "resolved"
    elif after == before:
        return "no_change"
    else:
        return "worsened"


def apply_fallback(cause, service, steps):
    if steps:
        return steps

    c = (cause or "").lower()
    s = (service or "").lower()

    if "redis" in c or "redis" in s:
        return ["docker restart redis"]

    if "fastapi" in c or "fastapi" in s:
        return ["docker restart fastapi-app"]

    if "postgres" in c or "database" in c:
        return ["docker restart postgres"]

    return []


# -----------------------------
# MAIN LOOP
# -----------------------------

def main_loop():
    global last_action_time, last_action_signature

    print("Starting Auto-Remediation System (Auto-Discovery Enabled)...")

    last_restart = {}

    while True:
        try:
            # 🔥 AUTO-DISCOVERY HEALTH CHECK
            containers = get_all_containers()
            containers = filter_containers(containers)

            for c in containers:
                if not is_container_running(c):
                    now = time.time()

                    if c in last_restart and now - last_restart[c] < 20:
                        continue

                    print(f"\n--- HEALTH CHECK ---")
                    print(f"{c} is DOWN → restarting")

                    subprocess.run(["docker", "restart", c])
                    last_restart[c] = now
                    time.sleep(3)

            # 🔥 RCA PART
            ts = get_latest_timestamp()

            if ts is None:
                time.sleep(5)
                continue

            result = run_rca(ts)

            cause = result.get("inferred_cause", "Unknown")
            steps = result.get("suggested_steps", [])
            service = result.get("affected_service", "unknown")

            print("\n--- RCA RESULT ---")
            print("Cause:", cause)
            print("Service:", service)
            print("Steps (LLM):", steps)

            steps = apply_fallback(cause, service, steps)
            print("Steps (final):", steps)

            if not steps:
                print("No action needed")
                time.sleep(10)
                continue

            now = time.time()
            signature = f"{cause}:{steps}"

            # Deduplication
            if signature == last_action_signature:
                if get_error_count() == 0:
                    print("Issue already resolved, skipping")
                    time.sleep(10)
                    continue

            elif now - last_action_time < COOLDOWN:
                print("Cooldown active")
                time.sleep(10)
                continue

            before = get_error_count()

            if before == 0:
                print("No active errors, skipping")
                time.sleep(10)
                continue

            print("Errors before:", before)

            for step in steps:
                execute_step(step)
                time.sleep(5)

            last_action_time = now

            status = evaluate_fix(before)
            print("Fix status:", status)

            if status == "resolved":
                last_action_signature = signature
            else:
                last_action_signature = None

            print("\nWaiting for next cycle...\n")
            time.sleep(10)

        except Exception as e:
            print("Error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main_loop()