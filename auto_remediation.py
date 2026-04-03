import subprocess
import time

from rca_engine import run_rca, get_latest_timestamp, get_error_count
from decision_engine import classify_risk, decide_action


last_action_time = 0
last_action_cause = None
COOLDOWN = 30


def execute_action(action):
    if action["type"] == "command":
        print("Executing:", action["value"])
        subprocess.run(action["value"])
    elif action["type"] == "approval":
        print("Suggested:", action["value"])
    else:
        print("No action required")


def check_if_resolved(before, after):
    if after < before:
        return "resolved"
    elif after == before:
        return "no_change"
    else:
        return "worsened"


def main_loop():
    global last_action_time, last_action_cause

    print("Starting Auto-Remediation System...")

    while True:
        try:
            ts = get_latest_timestamp()

            if ts is None:
                print("No logs yet")
                time.sleep(5)
                continue

            result = run_rca(ts)
            cause = result["inferred_cause"]

            print("\n--- RCA RESULT ---")
            print("Cause:", cause)

            risk = classify_risk(cause)
            print("Risk:", risk)

            action = decide_action(cause, risk)
            print("Decision:", action)

            current_time = time.time()

            # 🔴 SAME ISSUE HANDLING
            if cause == last_action_cause:
                print("Same issue detected again")

                current_errors = get_error_count()

                if current_errors == 0:
                    print("Issue already resolved, skipping")
                    time.sleep(10)
                    continue
                else:
                    print("Issue persists, retrying action")

            # 🔴 COOLDOWN
            elif current_time - last_action_time < COOLDOWN:
                print("Cooldown active")
                time.sleep(10)
                continue

            # 🔴 EXECUTION
            if action["type"] != "none":

                before = get_error_count()

                # 🔴 SAFETY CHECK
                if before == 0:
                    print("No active errors, skipping action")
                    time.sleep(10)
                    continue

                print("Errors before:", before)

                execute_action(action)

                last_action_time = current_time

                time.sleep(10)

                after = get_error_count()
                print("Errors after:", after)

                status = check_if_resolved(before, after)
                print("Fix status:", status)

                if status == "resolved":
                    last_action_cause = cause
                else:
                    last_action_cause = None

            else:
                print("No action")

            print("\nWaiting for next cycle...\n")
            time.sleep(10)

        except Exception as e:
            print("Error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main_loop()