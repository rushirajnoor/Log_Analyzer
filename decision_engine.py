def classify_risk(cause):

    cause_lower = cause.lower()

    if "no issue" in cause_lower:
        return "zero"

    if "redis" in cause_lower:
        return "low"

    if "database" in cause_lower:
        return "low"

    if "memory" in cause_lower:
        return "medium"

    return "zero"


def decide_action(cause, risk):

    cause_lower = cause.lower()

    # 🔴 HARD STOP if no issue
    if "no issue" in cause_lower:
        return {
            "type": "none",
            "value": None
        }

    if risk == "low":

        if "redis" in cause_lower:
            return {
                "type": "command",
                "value": ["docker", "restart", "redis"]
            }

        if "database" in cause_lower:
            return {
                "type": "command",
                "value": ["docker", "restart", "fastapi-app"]
            }

    elif risk == "medium":
        return {
            "type": "approval",
            "value": "Investigate memory issue"
        }

    return {
        "type": "none",
        "value": None
    }