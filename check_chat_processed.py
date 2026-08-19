import sys, os, subprocess
sys.path.insert(0, "/var/www/www-root/data/www/feedpilot")

env_out = subprocess.check_output(
    ["systemctl", "show", "feedpilot", "--property=EnvironmentFiles"], text=True
)
import re
env_file = re.search(r'EnvironmentFiles=(.*?) \(', env_out)
db_url = ""
if env_file:
    env_path = env_file.group(1).strip()
    try:
        with open(env_path) as f:
            for line in f:
                if "APP_DB_URL" in line:
                    db_url = line.strip().split("=", 1)[1]
    except PermissionError:
        pass

if not db_url:
    pid_out = subprocess.check_output(["systemctl", "show", "feedpilot", "--property=MainPID"], text=True)
    pid = pid_out.strip().split("=")[1]
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for item in f.read().split(b"\x00"):
                if b"APP_DB_URL" in item:
                    db_url = item.decode().split("=", 1)[1]
    except:
        pass

if not db_url:
    print("Cannot get DB URL"); sys.exit(1)

import psycopg, psycopg.rows
conn = psycopg.connect(db_url, row_factory=psycopg.rows.dict_row, autocommit=True)

# Check what kinds are in processed bucket
print("=== All kind/status in processed bucket (last_sent_at IS NOT NULL) ===")
rows = conn.execute("""
    SELECT kind, status, COUNT(*) as cnt
    FROM conversation_items
    WHERE last_sent_at IS NOT NULL
    GROUP BY kind, status ORDER BY kind, cnt DESC
""").fetchall()
for r in rows:
    print(f"  kind={r['kind']} status={r['status']} count={r['cnt']}")

# Check the specific question UID
print("\n=== Question QuFi-50B-KQKWGR8YBtB ===")
rows = conn.execute("""
    SELECT conversation_uid, kind, status, last_sent_at, last_message_at
    FROM conversation_items
    WHERE conversation_uid LIKE '%QuFi%'
""").fetchall()
for r in rows:
    print(f"  uid={r['conversation_uid']}")
    print(f"  kind={r['kind']} status={r['status']}")
    print(f"  last_sent_at={r['last_sent_at']}")
    print(f"  last_message_at={r['last_message_at']}")

# Check if any questions have kind='chat'
print("\n=== Any records with wrong kind? ===")
rows = conn.execute("""
    SELECT conversation_uid, kind, status
    FROM conversation_items
    WHERE (kind='chat' AND status IN ('answered_manual','answered_auto'))
       OR (kind='question' AND last_sent_at IS NOT NULL)
    LIMIT 10
""").fetchall()
for r in rows:
    print(f"  uid={r['conversation_uid'][:50]} kind={r['kind']} status={r['status']}")
