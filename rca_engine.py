import pandas as pd
from sqlalchemy import create_engine
from llm_rca import infer_with_llm

engine = create_engine("postgresql://loguser:password@localhost:5432/logdb")


def get_latest_timestamp():
    df = pd.read_sql("SELECT MAX(timestamp) as ts FROM logs;", engine)
    return df['ts'][0]


def get_logs_around(timestamp):
    query = f"""
    SELECT *
    FROM logs
    WHERE timestamp > EXTRACT(EPOCH FROM NOW() - interval '10 seconds');
    """
    return pd.read_sql(query, engine)


def run_rca(timestamp):

    df = get_logs_around(timestamp)

    print(
      "Fetched rows:",
      len(df)
    )


    if df.empty:

        return {
            "inferred_cause": ""
        }



    # -------------------
    # Broader anomaly detection
    # -------------------

    relevant_logs = df[
        df["level"].isin(["ERROR", "WARNING"])
    ]

    # fallback: detect error keywords even in INFO
    if relevant_logs.empty:

        keyword_logs = df[
            df["message"].str.contains(
                "error|fail|timeout|refused|unavailable",
                case=False,
                na=False
            )
        ]

        if not keyword_logs.empty:
            relevant_logs = keyword_logs


    if relevant_logs.empty:

        return {
            "inferred_cause":
            "No issue detected"
        }
    


    # -------------------
    # Sort newest first
    # -------------------

    relevant_logs = (
        relevant_logs
        .sort_values(
           by="timestamp",
           ascending=False
        )
    )


    # -------------------
    # Take richer context
    # -------------------


    top_logs = (
        relevant_logs
        .sort_values(by="timestamp", ascending=False)
        .groupby("service", group_keys=False)
        .head(2)
        .head(10)
        [["service", "message"]]
    )
    # -------------------
    # Extract structured signals
    # -------------------

    error_count = len(relevant_logs)

    services = relevant_logs["service"].value_counts().to_dict()

    signals = []

    if error_count > 20:
        signals.append("HIGH_ERROR_RATE")

    if any("redis" in s for s in services):
        signals.append("REDIS_INVOLVED")

    if any("frontend" in s for s in services):
        signals.append("FRONTEND_INVOLVED")

    signals_text = " | ".join(signals)

    # -------------------
    # Service-aware context
    # -------------------

    logs_text = "Signals: " + signals_text + "\n\n"

    for _, row in top_logs.iterrows():

        logs_text += (
            f"[{row['service']}] "
            f"{row['message']}\n"
        )

    # -------------------
    # Safe LLM call
    # -------------------

    try:

        result = infer_with_llm(
            logs_text
        )

        cause = result.get(
            "cause",
            "Possible service failure or dependency issue"
        )

    except Exception as e:

        print(
           "LLM failed:",
           e
        )

        cause = "Possible service failure or dependency issue"


    return {

        "inferred_cause": cause,
        "llm_service": result.get("service", "unknown")

    }