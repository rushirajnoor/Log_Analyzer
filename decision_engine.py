def classify_risk(cause):

    if "Redis connectivity issue" in cause:
        return "low"

    elif "Database connectivity issue" in cause:
        return "low"

    elif "High memory usage" in cause:
        return "medium"

    else:
        return "zero"


def decide_action(cause, risk):

    if risk == "low":

        if "Redis connectivity issue" in cause:
            return {
                "type": "command",
                "value": ["docker", "restart", "redis"]
            }

        if "Database connectivity issue" in cause:
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