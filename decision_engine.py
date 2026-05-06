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
    "cartservice": ["redis-cart"],
    "checkoutservice": [
        "paymentservice",
        "shippingservice",
        "emailservice",
        "cartservice"
    ],
    "paymentservice": [],
    "shippingservice": [],
    "emailservice": [],
    "productcatalogservice": [],
    "recommendationservice": [],
    "currencyservice": [],
    "adservice": [],
    "loadgenerator": [],
    "redis-cart": []
}

def get_best_action(service):

    import pandas as pd
    from sqlalchemy import create_engine
    import random

    engine = create_engine("postgresql://loguser:password@localhost:5432/logdb")

    try:
        df = pd.read_sql(f"""
            SELECT action, verification
            FROM remediation_history
            WHERE service = '{service}'
        """, engine)

        df["action"] = df["action"].str.replace(r"restart.*", "restart", regex=True)

        if df.empty:
            return "restart"

        # -------------------
        # Compute success rate
        # -------------------
        stats = df.groupby("action").agg(
            success_rate=("verification", lambda x: (x == "success").mean()),
            count=("verification", "count")
        ).reset_index()

        # -------------------
        # Filter weak data
        # -------------------
        stats = stats[stats["count"] >= 2]

        if stats.empty:
            return "restart"

        # -------------------
        # Choose best action
        # -------------------
        # -------------------
        # Apply scale penalty
        # -------------------
        stats["score"] = stats["success_rate"] - (stats["action"] == "scale_up") * 0.1

        best = stats.sort_values(by="score", ascending=False).iloc[0]
        best_action = best["action"]


        # -------------------
        # Exploration (fixed)
        # -------------------
        if random.random() < 0.1:
            alternate = "scale_up" if best_action == "restart" else "restart"
            print(f"Exploration: trying alternate action → {alternate}")
            return alternate

        print(f"Learning stats for {service}:")
        print(stats)

        return best_action

    except Exception as e:
        print("Learning error:", e)
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

    # -------------------
    # 1. Direct service down (highest priority)
    # -------------------
    if "service is down" in c:
        return "service_failure"

    # -------------------
    # 2. Dependency failures
    # -------------------
    if (
        "dependency" in c
        or "backend" in c
        or "connection refused" in c
        or "timeout" in c
        or "unavailable" in c
    ):
        return "dependency_failure"

    # -------------------
    # 3. Resource issues
    # -------------------
    if (
        "high cpu" in c
        or "high memory" in c
        or "oom" in c
    ):
        return "resource_failure"

    # -------------------
    # 4. Network / external issues
    # -------------------
    if (
        "dns" in c
        or "network" in c
        or "external" in c
        or "metadata" in c
    ):
        return "network_failure"

    # -------------------
    # 5. No issue / noise
    # -------------------
    if (
        "no issue detected" in c
        or "no logs" in c
    ):
        return "unknown_fault"

    # -------------------
    # fallback
    # -------------------
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

    c = (cause or "").lower()

    # -------------------
    # STRONG SIGNAL (Phase 13 fix)
    # -------------------
    if "service is down" in c:
        print("Confidence signal: service down (strong)")
        return "HIGH"

    score = 0

    # -------------------
    # Signal 1:
    # Cause strength
    # -------------------
    if (
        "cannot connect to redis" in c
        or "redis down" in c
        or "redis not responding" in c
        or "redis service is not responding" in c
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
        print("Confidence signal: service down")
        score += 3

    # -------------------
    # Signal 3:
    # Metrics anomalies
    # -------------------
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "pods"]
        ).decode()

        if "CrashLoopBackOff" in out:
            print("Confidence signal: CrashLoopBackOff")
            score += 3

        if "OOMKilled" in out:
            print("Confidence signal: OOMKilled")
            score += 3

    except:
        pass

    # -------------------
    # FINAL DECISION
    # -------------------
    print("Confidence score:", score)

    if score >= 6:
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
    # External / network issues (DO NOT force dependency)
    # -------------------
    if (
        "metadata" in c
        or "169.254" in c
        or "external" in c
        or "dns" in c
        or "network" in c
    ):
        print("External issue detected → skipping dependency resolution")
        return "frontend"


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
        # 🔥 force consistent path: frontend → cartservice
        print("Frontend issue → routing to cartservice first")
        service = "cartservice"


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
    # Apply dependency ONLY for real dependency cases
    # -------------------

    if (
        "dependency" in c
        or "redis" in c
        or "timeout" in c
    ):

        for parent, deps in DEPENDENCY_GRAPH.items():

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


def resolve_final_service(cause, failure_tracker, dependency_graph,llm_service=None):

    trace_steps = []

    c = (cause or "").lower()

    # -------------------
    # STEP 1: initial mapping
    # -------------------
    rule_service = fix_from_cause(cause)

    # -------------------
    # LLM-assisted decision
    # -------------------
    service = rule_service

    if llm_service and llm_service != "unknown":

        if not rule_service:
            service = llm_service
            trace_steps.append(f"LLM used (no rule match) → {llm_service}")

        elif llm_service == rule_service:
            trace_steps.append(f"LLM agrees with rule → {rule_service}")

        else:
            # 🔥 NEW: allow LLM override if rule is weak
            if "unknown" in (rule_service or ""):
                service = llm_service
                trace_steps.append(f"LLM override (weak rule) → {llm_service}")
            else:
                trace_steps.append(
                    f"LLM suggests {llm_service} but rule chose {rule_service} → keeping rule"
                )

    if not service:
        return "unresolved", trace_steps

    # -------------------
    # STEP 2: external issue detection
    # -------------------
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
        trace_steps.append("External issue detected → skipping dependency")

    # -------------------
    # STEP 3: dependency resolution
    # -------------------
    if not external_issue:

        root = get_root_dependency(service)

        if root != service:
            trace_steps.append(f"Dependency override → {service} → {root}")
            service = root

    # -------------------
    # STEP 4: correlation override
    # -------------------
    correlated = select_root_cause(
        service,
        cause,
        failure_tracker,
        dependency_graph
    )

    if correlated != service:
        trace_steps.append(f"Correlation override → {service} → {correlated}")
        service = correlated
    else:
        trace_steps.append(f"Correlation kept → {service}")

    # -------------------
    # FINAL
    # -------------------
    return service, trace_steps

def select_root_cause(service,cause, failure_tracker, dependency_graph):
    
    # 🔥 ignore correlation if cause is not dependency-related to graph
    if "metadata" in service or "169.254" in service:
        return service
    
    if "metadata" in cause or "external" in cause:
        return service

    visited = set()
    queue = [service]

    earliest_service = service
    earliest_time = failure_tracker.get(service, float("inf"))

    while queue:

        current = queue.pop(0)

        if current in visited:
            continue

        visited.add(current)

        t = failure_tracker.get(current)

        if t is not None and t < earliest_time:
            earliest_time = t
            earliest_service = current

        # traverse deeper dependencies
        deps = dependency_graph.get(current, [])
        queue.extend(deps)

    return earliest_service