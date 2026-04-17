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
    WHERE to_timestamp(timestamp)
    > NOW() - interval '30 seconds';
    """
    return pd.read_sql(query, engine)


def run_rca(timestamp):
    df = get_logs_around(timestamp)

    print("Fetched rows:", len(df))

    if df.empty:
        return {
            "inferred_cause": "No logs",
        }

    error_logs = df[df['level'] == 'ERROR']

    if error_logs.empty:
        return {
            "inferred_cause": "No issue detected",
        }

    # take latest logs
    error_logs = error_logs.sort_values(by="timestamp", ascending=False)

    logs_text = "\n".join(error_logs['message'].head(3).tolist())

    # 🔴 SAFE LLM CALL
    try:
        result = infer_with_llm(logs_text)
        cause = result.get("cause", "Unknown issue")

    except Exception as e:
        print("LLM failed:", e)
        cause = "Unknown issue"

    return {
        "inferred_cause": cause
    }