import subprocess
import time

from rca_engine import run_rca, get_latest_timestamp
from decision_engine import classify_risk, decide_action


# --- CONTROL VARIABLES ---
last_action_time = 0
last_action_cause = None
COOLDOWN = 30  # seconds


def execute_action(action):
    if action["type"] == "command":
        print("Executing:", action["value"])
        subprocess.run(action["value"])

    elif action["type"] == "approval":
        print("Suggested action:", action["value"])
        choice = input("Approve? (y/n): ")

        if choice.lower() == "y":
            print("Approved (manual execution required)")
        else:
            print("Skipped")

    else:
        print("No action required")


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

            # --- Risk classification ---
            risk = classify_risk(cause)
            print("Risk Level:", risk)

            # --- Decision ---
            action = decide_action(cause, risk)
            print("Decision:", action)

            current_time = time.time()

            # --- PREVENT REPEATED ACTIONS ---
            if cause == last_action_cause:
                print("Same issue already handled, skipping action")

            # --- COOLDOWN CHECK ---
            elif current_time - last_action_time < COOLDOWN:
                print("Cooldown active, skipping action")

            else:
                if action["type"] != "none":
                    execute_action(action)
                    last_action_time = current_time
                    last_action_cause = cause
                else:
                    print("No action executed")

            print("\nWaiting for next cycle...\n")
            time.sleep(10)

        except Exception as e:
            print("Error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main_loop()