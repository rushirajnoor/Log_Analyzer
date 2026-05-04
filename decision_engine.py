import time
import subprocess
from sqlalchemy import create_engine, text


engine = create_engine("postgresql://loguser:password@localhost:5432/logdb")


ACTIVE_INCIDENTS = {}

INCIDENT_TTL = 300

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


def get_best_action(service):

    try:
        with engine.begin() as conn:

            result = conn.execute(
                text(
                    """
                    SELECT action, COUNT(*) as cnt
                    FROM remediation_history
                    WHERE service=:svc
                    AND verification='success'
                    GROUP BY action
                    """
                ),
                {"svc": service}
            ).fetchall()

            if not result:
                return "restart"

            action_counts = {
                row[0]: row[1] for row in result
            }

            best_action = max(
                action_counts,
                key=action_counts.get
            )

            if "scale" in best_action:
                return "scale_up"

            if "restart" in best_action:
                return "restart"

            return "restart"

    except:
        return "restart"


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


def classify_fault(cause):

    c = (cause or "").lower()

    if (
        "frontend requests" in c
        or "frontend service" in c
        or "requests are failing" in c
    ):
        return "dependency_failure"

    if (
        "cannot connect to redis" in c
        or "dependency" in c
        or "redis down" in c
    ):
        return "dependency_failure"


    if (
        "high cpu" in c
        or "heavy load" in c
        or "oomkilled" in c
    ):
        return "resource_exhaustion"


    if (
        "rollout" in c
        or "deployment failed" in c
        or "config" in c
    ):
        return "deployment_fault"


    if (
        "dns" in c
        or "network" in c
    ):
        return "network_fault"

    
    if (
        "timeout" in c
        or "endpoint unreachable" in c
    ):
        return "network_fault"


    if (
        "maintenance" in c
    ):
        return "service_maintenance"


    if (
        "request error" in c
        or "unknown request error" in c
    ):
        return "dependency_failure"


    return "unknown_fault"



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
        or "redis not responding" in c
        or "redis service is not responding" in c
        or "service down" in c
    ):
        score += 4


    if (
        "request errors" in c
        or "requests are failing" in c
        or "multiple request" in c
        or "requests failing" in c
    ):
        score += 3


    # -------------------
    # Signal 2:
    # Health evidence
    # -------------------

    if (
        service
        and service != "unresolved"
        and not is_service_running(service)
    ):

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



def is_duplicate_incident(service, cause):

    if not service:
        return False


    # use structured fault classifier
    cause_class = classify_fault(cause)


    fingerprint = (
        service
        + "|"
        + cause_class
    )

    now = time.time()

    if fingerprint in ACTIVE_INCIDENTS:

        age = (
            now
            - ACTIVE_INCIDENTS[fingerprint]
        )

        if age < INCIDENT_TTL:

            print(
               "Duplicate incident detected"
            )

            return True


    ACTIVE_INCIDENTS[
       fingerprint
    ] = now

    return False


def prefer_scale_first(service):

    try:

        with engine.begin() as conn:

            result = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM remediation_history
                    WHERE service=:svc
                    AND action LIKE '%restart%'
                    AND verification='failed_escalated'
                    """
                ),
                {
                    "svc": service
                }
            )

            failures = result.scalar()


            if failures >= 1:

                print(
                  "Learning signal:"
                  " restart previously failed"
                )

                return True


            return False


    except:

        return False



def get_root_dependency(service):

    current = service

    while (
        current in DEPENDENCY_GRAPH
        and len(DEPENDENCY_GRAPH[current]) > 0
    ):
        current = DEPENDENCY_GRAPH[current][0]

    return current


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

    c = (cause or "").lower()


    # -------------------
    # Better cause mapping
    # -------------------

    if (
        "redis" in c
        or "cache" in c
    ):
        service = "redis-cart"


    elif (
        "frontend" in c
        or "frontend requests" in c
    ):
        service = "frontend"


    elif (
        "cartservice" in c
    ):
        service = "cartservice"


    elif (
        "payment" in c
    ):
        service = "paymentservice"


    elif (
        "timeout" in c
        or "backend not responding" in c
    ):
        service = "cartservice"

    elif (
        "frontend failing" in c
        or "frontend requests are failing" in c
    ):
        service = "frontend"

    else:

        # fallback to old resolver
        service = resolve_dependency(
            cause
        )


    if not service:
        return None


    # -------------------
    # KEEP dependency logic
    # -------------------

    for parent,deps in DEPENDENCY_GRAPH.items():

        if service == parent and deps:

            print(
               f"{service} depends on "
               f"{deps[0]} "
               "→ fixing dependency first"
            )

            return get_root_dependency(
                service
            )


    return service

def select_root_cause(service, failure_tracker, dependency_graph):
    """
    Choose earliest failing service along the dependency chain.
    If dependencies failed earlier than the current service,
    pick the earliest among them.
    """

    # if unknown service or no deps → return as is
    deps = dependency_graph.get(service, [])
    if not deps:
        return service

    # include self + its direct dependencies
    candidates = [service] + deps

    earliest_service = service
    earliest_time = failure_tracker.get(service, float("inf"))

    for s in candidates:
        t = failure_tracker.get(s)
        if t is not None and t < earliest_time:
            earliest_time = t
            earliest_service = s

    return earliest_service