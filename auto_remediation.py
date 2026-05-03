from decision_engine import (
    classify_fault,
    get_confidence,
    is_duplicate_incident,
    prefer_scale_first,
    is_service_running,
    fix_from_cause,
    get_root_dependency
)

import subprocess
import time
from sqlalchemy import create_engine, text
from rca_engine import run_rca, get_latest_timestamp

print("RUNNING FILE:", __file__)

engine = create_engine(
    "postgresql://loguser:password@localhost:5432/logdb"
)

CORRELATED_EVENTS = []
CORRELATION_WINDOW = 120

LAST_RESTART={}
RESTART_COOLDOWN=60

ACTION_COOLDOWN = 120
LAST_ACTION = {}

def incident_key(service, cause):

    return f"{service}:{cause}".lower()


def in_cooldown(service):

    now = time.time()

    last = LAST_ACTION.get(service, 0)

    if now - last < ACTION_COOLDOWN:
        print(f"{service} cooldown active")
        return True

    LAST_ACTION[service] = now
    return False


def log_remediation(service,cause,action,verification):
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO remediation_history
                    (service,cause,action,verification)
                    VALUES
                    (:s,:c,:a,:v)
                    """
                ),
                {
                    "s":service,
                    "c":cause,
                    "a":action,
                    "v":verification
                }
            )
    except Exception as e:
        print("History logging failed:",e)



def had_past_success(service):

    try:

        with engine.begin() as conn:

            result = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM remediation_history
                    WHERE service=:svc
                    AND verification='success'
                    """
                ),
                {
                    "svc": service
                }
            )

            count = result.scalar()

            return count > 0

    except:

        return False




def check_metrics():

    print("\n--- METRICS CHECK ---")

    # -------------------------
    # 1. CPU / Memory
    # -------------------------

    try:

        out = subprocess.check_output(
            ["kubectl","top","pods"],
            stderr=subprocess.DEVNULL
        ).decode().splitlines()

        for line in out[1:]:

            parts = line.split()

            if len(parts) < 3:
                continue

            pod = parts[0]

            cpu = parts[1].replace("m","")
            mem = parts[2].replace("Mi","")

            try:
                cpu=int(cpu)
                mem=int(mem)
            except:
                continue

            if cpu > 500:
                print(
                  f"HIGH CPU anomaly: {pod}"
                )

            if mem > 500:
                print(
                  f"HIGH MEMORY anomaly: {pod}"
                )

    except:
        print(
          "Metrics server unavailable"
        )


    # -------------------------
    # 2. Pod status anomalies
    # -------------------------

    try:

        out = subprocess.check_output(
            [
             "kubectl",
             "get",
             "pods"
            ]
        ).decode()

        if "CrashLoopBackOff" in out:

            print(
             "ANOMALY: CrashLoopBackOff detected"
            )

        if "OOMKilled" in out:

            print(
             "ANOMALY: OOMKilled detected"
            )

    except:
        pass



def restart(service,cause="health_check"):

    print(f"Restarting {service}")

    subprocess.run(
        [
            "kubectl",
            "rollout",
            "restart",
            f"deployment/{service}"
        ]
    )

    time.sleep(10)

    if is_service_running(service):
        print(f"Verification: {service} recovery successful")

        log_remediation(
            service,
            cause,
            f"restart {service}",
            "success"
        )

        return True

    else:
        print(f"Verification: {service} recovery failed")

        print("Primary remediation failed")

        print(
        "Trying secondary remediation: scale up"
        )

        scaled = scale_up(service)

        if not scaled:

            print(
                "Secondary remediation failed"
            )

            print(
                "Trying tertiary remediation: rollback"
            )

            rolled_back = rollback(service)

            if not rolled_back:

                print(
                    f"ESCALATION: manual intervention needed for {service}"
                )

        log_remediation(
            service,
            cause,
            f"restart {service}",
            "failed_escalated"
        )

        return False


def get_all_deployments():
    try:
        out = subprocess.check_output(
            [
                "kubectl",
                "get",
                "deployments",
                "-o",
                "jsonpath={.items[*].metadata.name}"
            ]
        ).decode()

        return out.split()

    except:
        return []


def check_and_fix_services():

    monitored = get_all_deployments()

    now = time.time()

    for svc in monitored:

        if not is_service_running(svc):

            if (
                svc in LAST_RESTART and
                now - LAST_RESTART[svc] < RESTART_COOLDOWN
            ):

                print(
                    f"{svc} restart cooldown active"
                )

                continue

            print("\n--- HEALTH CHECK ---")
            print(
                f"{svc} is DOWN → restarting"
            )

            if prefer_scale_first(svc):

                print(
                    "Adaptive decision:"
                    " going directly to scale"
                )

                scale_up(svc)

            else:

                restart(
                    svc,
                    "health_check_down"
                )

            LAST_RESTART[svc] = now

            time.sleep(3)


def scale_up(service):

    print(
        f"Scaling {service} to 2 replicas"
    )

    subprocess.run(
        [
            "kubectl",
            "scale",
            f"deployment/{service}",
            "--replicas=2"
        ]
    )

    time.sleep(10)

    if is_service_running(service):

        print(
            f"Scale remediation successful for {service}"
        )

        log_remediation(
            service,
            "verification_failed",
            "scale_up",
            "success"
        )

        return True

    else:

        print(
            f"Scale remediation failed for {service}"
        )

        return False


def rollback(service):

    print(
        f"Trying rollback for {service}"
    )

    subprocess.run(
        [
            "kubectl",
            "rollout",
            "undo",
            f"deployment/{service}"
        ]
    )

    time.sleep(10)

    if is_service_running(service):

        print(
            f"Rollback successful for {service}"
        )

        log_remediation(
            service,
            "scale_failed",
            "rollback",
            "success"
        )

        return True

    else:

        print(
            f"Rollback failed for {service}"
        )

        return False




def check_incident_correlation(service,fault):

    now = time.time()

    CORRELATED_EVENTS.append(
        (
          now,
          service,
          fault
        )
    )

    # keep only recent events
    recent = []

    for e in CORRELATED_EVENTS:

        if (
           now - e[0]
           < CORRELATION_WINDOW
        ):
            recent.append(e)

    CORRELATED_EVENTS[:] = recent


    services = set(
       e[1] for e in recent
    )

    faults = set(
       e[2] for e in recent
    )


    if (
       len(services) >= 2
       and len(faults) >= 1
    ):

        print(
          "Correlated multi-service incident detected"
        )

        return True


    return False

def main():

    print(
      "Starting Dependency-Aware Auto-Remediation..."
    )

    while True:

        try:

            # -------------------
            # Metrics layer
            # -------------------

            check_metrics()


            # -------------------
            # Health layer
            # -------------------

            check_and_fix_services()


            # -------------------
            # Log-based RCA
            # -------------------

            ts = get_latest_timestamp()

            if ts is None:

                print(
                  "No logs yet"
                )

                time.sleep(5)

                continue


            result = run_rca(ts)


            cause = result.get(
                "inferred_cause",
                "Unknown"
            )


            print(
              "\n--- RCA RESULT ---"
            )

            print(
              "Cause:",
              cause
            )


            # -------------------
            # Fault classification
            # -------------------

            fault = classify_fault(
                cause
            )

            print(
                "Fault Class:",
                fault
            )

            # -------------------
            # Service resolution
            # -------------------

            service = fix_from_cause(
                cause
            )
            # 🔥 force root dependency resolution
            if service:
                root = get_root_dependency(service)

                if root != service:
                    print(f"{service} depends on {root} → fixing dependency first")
                    service = root

            if not service:

                service = "unresolved"


            print(
                "DEBUG service:",
                service
            )


            if service != "unresolved":

                correlated = check_incident_correlation(
                    service,
                    fault
                )

                if correlated:

                    print(
                        "Incident correlation active"
                    )



            # -------------------
            # Confidence-aware gating
            # -------------------

            confidence = get_confidence(
                cause,
                service
            )


            print(
               "Confidence:",
               confidence
            )


            if confidence == "LOW":

                print(
                  "Low confidence -> no auto-remediation"
                )

                time.sleep(10)

                continue


            # -------------------
            # Deduplication
            # -------------------

            if "no issue detected" not in cause.lower():

                # 🔴 First observation → do nothing
                if not is_duplicate_incident(service, cause):

                    print("First observation → waiting for confirmation")

                    time.sleep(5)
                    continue


            # -------------------
            # Unresolved guard
            # -------------------

            if service == "unresolved":

                print(
                  "No action needed"
                )

                time.sleep(10)

                continue


            # -------------------
            # Remediation
            # -------------------

            # 🔥 cooldown check BEFORE action
            if in_cooldown(service):
                time.sleep(5)
                continue

            print(
              f"{service} -> restarting "
              "(dependency-aware recovery)"
            )

            if prefer_scale_first(service):

                print(
                    "Adaptive decision:"
                    " going directly to scale"
                )

                scale_up(service)

            else:

                restart(
                    service,
                    cause
                )

            print("\nWaiting...\n")

            time.sleep(10)


        except Exception as e:

            print(
              "Error:",
              e
            )

            time.sleep(5)



if __name__ == "__main__":

    main()