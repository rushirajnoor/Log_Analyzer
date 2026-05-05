from decision_engine import (
    classify_fault,
    get_confidence,
    is_duplicate_incident,
    prefer_scale_first,
    is_service_running,
    fix_from_cause,
    get_root_dependency,
    get_best_action,
    select_root_cause,
    DEPENDENCY_GRAPH
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

FAILURE_TRACKER = {}   # service -> first_seen_ts

def trace(msg):
    print(f"[TRACE] {msg}")

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

# -----------------------------
# Incident lifecycle functions
# -----------------------------

def create_incident_if_not_exists(service, cause, fault):

    try:
        with engine.begin() as conn:

            result = conn.execute(
                text(
                    """
                    SELECT id FROM incidents
                    WHERE service=:s
                    AND status='active'
                    LIMIT 1
                    """
                ),
                {"s": service}
            ).fetchone()

            if result:
                # already active
                return

            conn.execute(
                text(
                    """
                    INSERT INTO incidents
                    (service, cause, fault_class, status)
                    VALUES (:s, :c, :f, 'active')
                    """
                ),
                {
                    "s": service,
                    "c": cause,
                    "f": fault
                }
            )

            print(f"[INCIDENT] Created for {service}")

    except Exception as e:
        print("Incident creation failed:", e)


def mark_incident_resolved(service):

    try:
        with engine.begin() as conn:

            conn.execute(
                text(
                    """
                    UPDATE incidents
                    SET status='resolved',
                        resolved_at=NOW()
                    WHERE service=:s
                    AND status='active'
                    """
                ),
                {"s": service}
            )

            print(f"[INCIDENT] Resolved for {service}")

    except Exception as e:
        print("Incident resolution failed:", e)



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
            ["kubectl", "top", "pods"],
            stderr=subprocess.DEVNULL
        ).decode().splitlines()

        for line in out[1:]:

            parts = line.split()

            if len(parts) < 3:
                continue

            pod = parts[0]

            # 🔥 extract service name
            service = pod.split("-")[0]

            cpu = parts[1].replace("m", "")
            mem = parts[2].replace("Mi", "")

            try:
                cpu = int(cpu)
                mem = int(mem)
            except:
                continue

            # -------------------------
            # CPU anomaly
            # -------------------------

            if cpu > 500:
                print(f"HIGH CPU anomaly: {pod}")

                # 🔥 feed correlation system (first-seen only)
                if service not in FAILURE_TRACKER:
                    FAILURE_TRACKER[service] = time.time()

                print(f"Prediction: {service} may degrade soon (CPU spike)")

            # -------------------------
            # Memory anomaly
            # -------------------------

            if mem > 500:
                print(f"HIGH MEMORY anomaly: {pod}")

                if service not in FAILURE_TRACKER:
                    FAILURE_TRACKER[service] = time.time()

                print(f"Prediction: {service} may degrade soon (Memory spike)")

    except:
        print("Metrics server unavailable")


    # -------------------------
    # 2. Pod status anomalies
    # -------------------------

    try:

        out = subprocess.check_output(
            ["kubectl", "get", "pods"]
        ).decode().splitlines()

        for line in out[1:]:

            parts = line.split()

            if len(parts) < 3:
                continue

            pod = parts[0]
            status = parts[2]

            service = pod.split("-")[0]

            if "CrashLoopBackOff" in status:

                print(f"ANOMALY: {pod} in CrashLoopBackOff")

                if service not in FAILURE_TRACKER:
                    FAILURE_TRACKER[service] = time.time()

            if "OOMKilled" in line:

                print(f"ANOMALY: {pod} OOMKilled")

                if service not in FAILURE_TRACKER:
                    FAILURE_TRACKER[service] = time.time()

    except:
        pass


def restart(service, cause="health_check"):

    print(f"Restarting {service}")

    start_time = time.time()

    subprocess.run(
        [
            "kubectl",
            "rollout",
            "restart",
            f"deployment/{service}"
        ]
    )

    time.sleep(10)

    success = is_service_running(service)

    recovery_time = time.time() - start_time

    if success:

        print(f"Verification: {service} recovery successful")

        mark_incident_resolved(service)
        # clear failure signal after recovery
        if service in FAILURE_TRACKER:
            del FAILURE_TRACKER[service]

        log_remediation(
            service,
            cause,
            f"restart {service}",
            "success"
        )

        log_evaluation(
            service,
            cause,
            classify_fault(cause),
            get_confidence(cause, service),
            "restart",
            True,
            recovery_time
        )

        return True

    else:

        print(f"Verification: {service} recovery failed")

        print("Primary remediation failed")

        print("Trying secondary remediation: scale up")

        scaled = scale_up(service)

        log_remediation(
            service,
            cause,
            f"restart {service}",
            "failed_escalated"
        )

        log_evaluation(
            service,
            cause,
            classify_fault(cause),
            get_confidence(cause, service),
            "restart",
            False,
            recovery_time
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

    print(f"Scaling {service} to 2 replicas")

    start_time = time.time()

    subprocess.run(
        [
            "kubectl",
            "scale",
            f"deployment/{service}",
            "--replicas=2"
        ]
    )

    time.sleep(10)

    success = is_service_running(service)

    recovery_time = time.time() - start_time

    if success:

        print(f"Scale remediation successful for {service}")

        mark_incident_resolved(service)
        # clear failure signal after recovery
        if service in FAILURE_TRACKER:
            del FAILURE_TRACKER[service]

        log_remediation(
            service,
            "verification_failed",
            "scale_up",
            "success"
        )

        log_evaluation(
            service,
            "scale_trigger",
            "resource_action",
            "MEDIUM",
            "scale_up",
            True,
            recovery_time
        )

        return True

    else:

        print(f"Scale remediation failed for {service}")

        log_evaluation(
            service,
            "scale_trigger",
            "resource_action",
            "MEDIUM",
            "scale_up",
            False,
            recovery_time
        )

        return False


def rollback(service):

    print(f"Trying rollback for {service}")

    start_time = time.time()

    subprocess.run(
        [
            "kubectl",
            "rollout",
            "undo",
            f"deployment/{service}"
        ]
    )

    time.sleep(10)

    success = is_service_running(service)

    recovery_time = time.time() - start_time

    if success:

        print(f"Rollback successful for {service}")

        mark_incident_resolved(service)
        # clear failure signal after recovery
        if service in FAILURE_TRACKER:
            del FAILURE_TRACKER[service]

        log_remediation(
            service,
            "scale_failed",
            "rollback",
            "success"
        )

        log_evaluation(
            service,
            "rollback_trigger",
            "deployment_fault",
            "MEDIUM",
            "rollback",
            True,
            recovery_time
        )

        return True

    else:

        print(f"Rollback failed for {service}")

        log_evaluation(
            service,
            "rollback_trigger",
            "deployment_fault",
            "MEDIUM",
            "rollback",
            False,
            recovery_time
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


# -----------------------------
# Evaluation logging
# -----------------------------

def log_evaluation(service, cause, fault, confidence, action, success, recovery_time):

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO evaluation_logs
                    (service, cause, fault_class, confidence, action, success, recovery_time)
                    VALUES
                    (:s, :c, :f, :conf, :a, :succ, :rt)
                    """
                ),
                {
                    "s": service,
                    "c": cause,
                    "f": fault,
                    "conf": confidence,
                    "a": action,
                    "succ": success,
                    "rt": recovery_time
                }
            )
    except Exception as e:
        print("Evaluation logging failed:", e)

def main():

    print("Starting Dependency-Aware Auto-Remediation...")

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
                print("No logs yet")
                time.sleep(5)
                continue

            result = run_rca(ts)

            cause = result.get("inferred_cause", "Unknown")

            print("\n--- RCA RESULT ---")
            print("Cause:", cause)
            trace(f"RCA cause → {cause}")

            # -------------------
            # Fault classification
            # -------------------
            fault = classify_fault(cause)

            print("Fault Class:", fault)
            trace(f"Fault classified as → {fault}")

            # -------------------
            # Service resolution
            # -------------------
            service = fix_from_cause(cause)

            # -------------------
            # Detect external case
            # -------------------
            c = (cause or "").lower()

            external_issue = False

            if (
                "metadata" in c
                or "169.254" in c
                or "dns" in c
                or "external" in c
                or "connection refused" in c
                or "connection error" in c
                or "unable to connect" in c
            ):
                external_issue = True



            trace(f"Initial service from cause mapping → {service}")

            # -------------------
            # Dependency resolution
            # -------------------

            if not service:
                service = "unresolved"

            elif external_issue:
                trace("Dependency resolution skipped due to external issue")

            else:
                root = get_root_dependency(service)

                if root != service:
                    print(f"{service} depends on {root} → fixing dependency first")
                    trace(f"Dependency override → {service} → {root}")
                    service = root


            print("DEBUG service:", service)
            trace(f"After dependency resolution → {service}")



            # -------------------
            # Correlation (incident-level)
            # -------------------
            if service != "unresolved":
                correlated_flag = check_incident_correlation(service, fault)
                if correlated_flag:
                    print("Incident correlation active")
                    trace(f"Incident correlation signal detected for {service}")

            # -------------------
            # Causal correlation (time + deps)
            # -------------------
            correlated = select_root_cause(
                service,
                cause,
                FAILURE_TRACKER,
                DEPENDENCY_GRAPH
            )

            if correlated != service:
                print(f"Correlation override: {service} → {correlated}")
                trace(f"Causal correlation override → {service} → {correlated}")
                service = correlated
            else:
                trace(f"Causal correlation kept service → {service}")

            trace(f"Final service after correlation → {service}")

            # -------------------
            # Track first-seen failure
            # -------------------
            if (
                service
                and service != "unresolved"
                and "no issue detected" not in cause.lower()
            ):
                if service not in FAILURE_TRACKER:
                    FAILURE_TRACKER[service] = time.time()

            # -------------------
            # Confidence
            # -------------------
            confidence = get_confidence(cause, service)

            print("Confidence:", confidence)
            trace(f"Confidence decision → {confidence}")

            # -------------------
            # Deduplication
            # -------------------
            if "no issue detected" not in cause.lower():

                if not is_duplicate_incident(service, cause):
                    print("First observation → waiting for confirmation")
                    trace("Dedup: first observation → waiting (no action)")
                    time.sleep(5)
                    continue
                else:
                    trace("Dedup: confirmed incident → proceeding")

            # -------------------
            # Confidence gate
            # -------------------
            if confidence == "LOW":
                print("Low confidence -> no auto-remediation")
                trace("Confidence LOW → skipping remediation")
                time.sleep(10)
                continue

            # -------------------
            # Incident tracking
            # -------------------
            if (
                service != "unresolved"
                and "no issue detected" not in cause.lower()
            ):
                create_incident_if_not_exists(service, cause, fault)

            # -------------------
            # Unresolved guard
            # -------------------
            if service == "unresolved":
                print("No action needed")
                trace("Service unresolved → skipping action")
                time.sleep(10)
                continue

            # -------------------
            # Cooldown check
            # -------------------
            if in_cooldown(service):
                trace(f"Cooldown active for {service} → skipping")
                time.sleep(5)
                continue

            # -------------------
            # Action selection
            # -------------------
            trace(f"Evaluating best action for service → {service}")

            best_action = get_best_action(service)

            print(f"Adaptive decision: {best_action}")
            trace(f"Action chosen → {best_action} (based on history)")

            # -------------------
            # Execute action
            # -------------------
            trace(f"Executing action → {best_action} on {service}")

            if best_action == "scale_up":
                scale_up(service)
            else:
                restart(service, cause)

            print("\nWaiting...\n")
            time.sleep(10)

        except Exception as e:
            print("Error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()