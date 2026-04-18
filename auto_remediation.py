import subprocess
import time
from sqlalchemy import create_engine, text
from rca_engine import run_rca, get_latest_timestamp

print("RUNNING FILE:", __file__)

engine = create_engine(
    "postgresql://loguser:password@localhost:5432/logdb"
)

DEPENDENCIES = {
    "frontend": ["cartservice"],
    "cartservice": ["redis-cart"],
    "checkoutservice": ["paymentservice"],
}

LAST_RESTART={}
RESTART_COOLDOWN=60


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


def check_and_fix_services():

    monitored=[
        "frontend",
        "cartservice",
        "redis-cart"
    ]

    now=time.time()

    for svc in monitored:

        if not is_service_running(svc):

            if (
                svc in LAST_RESTART and
                now-LAST_RESTART[svc] < RESTART_COOLDOWN
            ):
                print(f"{svc} restart cooldown active")
                continue

            print("\n--- HEALTH CHECK ---")
            print(f"{svc} is DOWN → restarting")

            restart(svc,"health_check_down")

            LAST_RESTART[svc]=now

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
            return deps[0]

    return service


def main():

    print("Starting Dependency-Aware Auto-Remediation...")

    while True:

        try:

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

            service=fix_from_cause(cause)

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