import psycopg2
import pandas as pd

conn = psycopg2.connect(
    dbname="logdb",
    user="loguser",
    password="password",
    host="localhost"
)

query = """
SELECT 
    to_timestamp(timestamp) as time,
    level
FROM logs;
"""

df = pd.read_sql(query, conn)

# Convert to time window counts
df['time'] = pd.to_datetime(df['time'])

# group per minute
features = df.groupby([
    pd.Grouper(key='time', freq='1min'),
    'level'
]).size().unstack(fill_value=0)

print(features)