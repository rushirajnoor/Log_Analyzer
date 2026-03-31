import pandas as pd
from sqlalchemy import create_engine

engine = create_engine("postgresql://loguser:password@localhost:5432/logdb")


def get_latest_timestamp():
    query = "SELECT MAX(timestamp) as ts FROM logs;"
    df = pd.read_sql(query, engine)
    return df['ts'][0]


def get_logs_around(timestamp):
    query = f"""
    SELECT *
    FROM logs
    WHERE to_timestamp(timestamp)
    BETWEEN to_timestamp({timestamp}) - interval '5 minutes'
    AND to_timestamp({timestamp}) + interval '5 minutes';
    """
    return pd.read_sql(query, engine)


def analyze_patterns(df):
    return df.groupby(['level', 'service', 'message']).size().reset_index(name='count')


def infer_cause(grouped):
    if grouped.empty:
        return "No clear root cause found", grouped

    weights = {"ERROR": 3, "WARNING": 2, "INFO": 1}

    grouped['score'] = grouped.apply(
        lambda row: row['count'] * weights.get(row['level'], 1),
        axis=1
    )

    grouped = grouped.sort_values(by='score', ascending=False)

    message = grouped.iloc[0]['message'].lower()

    if "redis" in message:
        cause = "Redis connectivity issue"
    elif "database" in message:
        cause = "Database connectivity issue"
    elif "memory" in message:
        cause = "High memory usage"
    else:
        cause = "Unknown issue"

    return cause, grouped


def run_rca(timestamp):
    df = get_logs_around(timestamp)

    print("Fetched rows:", len(df))

    if df.empty:
        return {"top_patterns": [], "inferred_cause": "No logs found"}

    grouped = analyze_patterns(df)
    cause, scored = infer_cause(grouped)

    return {
        "top_patterns": scored.head(5).to_dict(orient="records"),
        "inferred_cause": cause
    }


def get_error_count():
    query = """
    SELECT COUNT(*) as count
    FROM logs
    WHERE level = 'ERROR'
    AND to_timestamp(timestamp) > NOW() - interval '2 minutes';
    """
    df = pd.read_sql(query, engine)
    return df['count'][0]