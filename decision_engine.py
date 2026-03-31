def classify_risk(cause):
    """
    Assign risk level based on inferred cause
    """

    if "Database connectivity issue" in cause:
        return "low"

    elif "High memory usage" in cause:
        return "medium"

    elif "Authentication" in cause:
        return "zero"

    else:
        return "zero"


def decide_action(cause, risk):
    """
    Decide what to do based on cause + risk
    """

    if risk == "low":
        # Safe to automate
        if "Database connectivity issue" in cause:
            return {
                "type": "command",
                "value": ["docker", "restart", "log-container"]
            }

    elif risk == "medium":
        # Needs approval
        return {
            "type": "approval",
            "value": "Restart container due to high memory usage"
        }

    else:
        return {
            "type": "none",
            "value": None
        }