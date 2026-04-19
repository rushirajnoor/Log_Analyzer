import subprocess
import time
from sqlalchemy import create_engine, text
from rca_engine import run_rca, get_latest_timestamp

print("RUNNING FILE:", __file__)

engine = create_engine(
    "postgresql://loguser:password@localhost:5432/logdb"
)

LAST_RESTART={}
RESTART_COOLDOWN=60

DEPENDENCY_GRAPH = {
    "frontend": [
        "cartservice",
        "productcatalogservice",
        "recommendationservice"
    ],

    "cartservice": [
        "redis-cart"
    ],

    "checkoutservice": [
        "paymentservice",
        "shippingservice",
        "emailservice"
    ],

    "productcatalogservice": [],

    "recommendationservice": [],

    "paymentservice": [],

    "shippingservice": [],

    "emailservice": [],

    "redis-cart": []
}


def get_root_dependency(service):

    current = service

    while (
        current in DEPENDENCY_GRAPH
        and len(DEPENDENCY_GRAPH[current]) > 0
    ):
        current = DEPENDENCY_GRAPH[current][0]

    return current


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



def get_confidence(cause, service):

    score = 0

    c = (cause or "").lower()


    # -------------------
    # Signal 1:
    # Cause strength
    # -------------------

    if (
        "cannot connect to redis" in c
        or "redis down" in c
        or "service down" in c
    ):
        score += 4


    if (
        "request errors" in c
    ):
        score += 3


    # -------------------
    # Signal 2:
    # Health evidence
    # -------------------

    if service and not is_service_running(service):

        print(
            "Confidence signal: service down"
        )

        score += 3


    # -------------------
    # Signal 3:
    # Metrics anomalies
    # -------------------

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
              "Confidence signal: CrashLoopBackOff"
            )

            score += 3


        if "OOMKilled" in out:

            print(
              "Confidence signal: OOMKilled"
            )

            score += 3

    except:
        pass


    # -------------------
    # Signal 4:
    # Historical support
    # -------------------

    if service and had_past_success(service):

        print(
          "Confidence signal: past success"
        )

        score += 3


    # -------------------
    # Final mapping
    # -------------------

    print(
       "Confidence score:",
       score
    )


    if score >= 7:
        return "HIGH"

    elif score >= 3:
        return "MEDIUM"

    else:
        return "LOW"


    # ambiguous but somewhat meaningful
    if (
        "request errors" in c
        or "multiple service issue" in c
    ):
        return "MEDIUM"


    # vague / weak / unknown
    return "LOW"


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

def get_all_pods():
    try:
        out=subprocess.check_output(
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


def is_service_running(service_name):
    pods=get_all_pods()
    for p in pods:
        if service_name in p:
            return True
    return False


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

            restart(
                svc,
                "health_check_down"
            )

            LAST_RESTART[svc] = now

            time.sleep(3)


def resolve_dependency(cause):

    c=(cause or "").lower()

    if "cartservice" in c:
        return "cartservice"

    if "redis down" in c:
        return "redis-cart"

    if "cannot connect to redis" in c:
        return "redis-cart"

    if "payment" in c:
        return "paymentservice"

    if "request errors" in c:
        return None

    if "multiple service issue" in c:
        return None

    if "potential service issue" in c:
        return None

    return None


def fix_from_cause(cause):

    service=resolve_dependency(cause)

    if not service:
        return None

    for parent,deps in DEPENDENCIES.items():

        if service==parent and deps:
            print(
                f"{service} depends on {deps[0]} → fixing dependency first"
            )
            return get_root_dependency(service)

    return service


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


def main():

    print("Starting Dependency-Aware Auto-Remediation...")

    while True:

        try:

            check_metrics()
            check_and_fix_services()

            ts=get_latest_timestamp()

            if ts is None:
                print("No logs yet")
                time.sleep(5)
                continue

            result=run_rca(ts)

            cause=result.get(
                "inferred_cause",
                "Unknown"
            )

            print("\n--- RCA RESULT ---")
            print("Cause:",cause)

            service = fix_from_cause(cause)
            confidence = get_confidence(cause,service)

            print(
                "Confidence:",
                confidence
            )



            if not service:
                print("No action needed")
                time.sleep(10)
                continue

            print(f"{service} → restarting (dependency-aware recovery)")

            restart(service,cause)

            print("\nWaiting...\n")
            time.sleep(10)

        except Exception as e:
            print("Error:",e)
            time.sleep(5)


if __name__=="__main__":
    main()