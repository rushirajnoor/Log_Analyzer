import pandas as pd
from sqlalchemy import create_engine
from llm_rca import infer_with_llm

engine = create_engine("postgresql://loguser:password@localhost:5432/logdb")


def get_latest_timestamp():
    query = "SELECT MAX(timestamp) as ts FROM logs;"
    df = pd.read_sql(query, engine)
    return df['ts'][0]


# 🔥 FIXED: Tight sliding window (recent logs only)
def get_logs_around(timestamp):
    query = f"""
    SELECT *
    FROM logs
    WHERE to_timestamp(timestamp)
    BETWEEN to_timestamp({timestamp}) - interval '30 seconds'
    AND to_timestamp({timestamp}) + interval '5 seconds';
    """
    return pd.read_sql(query, engine)


def analyze_patterns(df):
    return df.groupby(['level', 'service', 'message']).size().reset_index(name='count')


def run_rca(timestamp):
    df = get_logs_around(timestamp)

    print("Fetched rows:", len(df))

    if df.empty:
        return {
            "top_patterns": [],
            "inferred_cause": "No logs found"
        }

    grouped = analyze_patterns(df)

    # 🔥 ONLY ERROR LOGS TO LLM
    error_logs = df[df['level'] == 'ERROR']

    if error_logs.empty:
        return {
            "top_patterns": grouped.head(5).to_dict(orient="records"),
            "inferred_cause": "No issue detected"
        }

    # 🔥 take most recent ERROR logs (not random top)
    error_logs = error_logs.sort_values(by="timestamp", ascending=False)

    top_logs = error_logs['message'].head(3).tolist()
    logs_text = "\n".join(top_logs)

    cause = infer_with_llm(logs_text)

    return {
        "top_patterns": grouped.head(5).to_dict(orient="records"),
        "inferred_cause": cause
    }


def get_error_count():
    query = """
    SELECT COUNT(*) as count
    FROM logs
    WHERE level = 'ERROR'
    AND to_timestamp(timestamp) > NOW() - interval '30 seconds';
    """
    df = pd.read_sql(query, engine)
    return df['count'][0]


if __name__ == "__main__":
    ts = get_latest_timestamp()

