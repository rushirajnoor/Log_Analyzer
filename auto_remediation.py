import subprocess
import time

from rca_engine import run_rca, get_latest_timestamp, get_error_count

COOLDOWN = 30
last_action_time = 0
last_action_signature = None

ALLOWED_COMMANDS = ["docker"]


def execute_step(step):
    parts = step.split()

    if parts[0] not in ALLOWED_COMMANDS:
        print("Blocked unsafe step:", step)
        return

    print("Executing step:", parts)
    subprocess.run(parts)


def main_loop():
    global last_action_time, last_action_signature

    print("Starting Agentic Remediation System...")

    while True:
        try:
            ts = get_latest_timestamp()

            if ts is None:
                time.sleep(5)
                continue

            result = run_rca(ts)

            cause = result.get("inferred_cause", "Unknown")
            steps = result.get("suggested_steps", [])   # 🔥 CORRECT KEY

            print("\n--- RCA RESULT ---")
            print("Cause:", cause)
            print("Steps:", steps)

            # 🔥 FINAL FALLBACK (CRITICAL FIX)
            if not steps:
                cause_lower = cause.lower()

                if "redis" in cause_lower:
                    steps = ["docker restart redis"]

            if not steps:
                print("No action needed")
                time.sleep(10)
                continue

            current_time = time.time()

            action_signature = f"{cause}:{steps}"

            # 🔴 SAME ISSUE HANDLING
            if action_signature == last_action_signature:
                print("Same issue detected again")

                if get_error_count() == 0:
                    print("Issue already resolved, skipping")
                    time.sleep(10)
                    continue
                else:
                    print("Issue persists, retrying")

            # 🔴 COOLDOWN
            elif current_time - last_action_time < COOLDOWN:
                print("Cooldown active")
                time.sleep(10)
                continue

            before = get_error_count()

            if before == 0:
                print("No active errors, skipping")
                time.sleep(10)
                continue

            print("Errors before:", before)

            # 🔥 Execute steps
            for step in steps:
                execute_step(step)
                time.sleep(5)

            last_action_time = current_time

            # 🔥 Stabilization wait
            time.sleep(10)

            time.sleep(10)
            after1 = get_error_count()

            time.sleep(5)
            after2 = get_error_count()

            after = min(after1, after2)
            print("Errors after:", after)

            if after < before:
                print("Fix successful")
                last_action_signature = action_signature
            else:
                print("Fix may have failed")
                last_action_signature = None

            print("\nWaiting for next cycle...\n")
            time.sleep(10)

        except Exception as e:
            print("Error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main_loop()