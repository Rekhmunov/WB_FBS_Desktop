import sys, os, subprocess
sys.path.insert(0, "/var/www/www-root/data/www/feedpilot")

# Get DB URL from systemd env
env_out = subprocess.check_output(
    ["systemctl", "show", "feedpilot", "--property=Environment"], text=True
)
db_url = ""
for part in env_out.strip().replace("Environment=", "").split():
    if "APP_DB_URL" in part:
        db_url = part.split("=", 1)[1]
        break

if not db_url:
    print("No DB URL found in systemd env")
    sys.exit(1)

import psycopg, psycopg.rows
conn = psycopg.connect(db_url, row_factory=psycopg.rows.dict_row, autocommit=True)

# Count questions by status
print("=== Questions by status ===")
rows = conn.execute(
    "SELECT kind, status, COUNT(*) as cnt FROM conversation_items WHERE kind='question' GROUP BY kind, status"
).fetchall()
for r in rows:
    print(f"  kind={r['kind']} status={r['status']} count={r['cnt']}")

# Check what's in the date range
print("\n=== Questions in date range 2026-04-09 to 2026-05-09 ===")
rows = conn.execute(
    """SELECT conversation_uid, kind, status, updated_at, last_message_at,
       message_text, customer_name
       FROM conversation_items
       WHERE kind='question'
       AND updated_at::date >= '2026-04-09'::date
       AND updated_at::date <= '2026-05-09'::date
       LIMIT 5"""
).fetchall()
print(f"  Count: {len(rows)}")
for r in rows:
    print(f"  uid={r['conversation_uid'][:40]} status={r['status']} updated={r['updated_at'][:10]}")
    print(f"    text={str(r['message_text'])[:60]}")

# Check ALL questions regardless of date
print("\n=== ALL questions ===")
rows = conn.execute(
    "SELECT conversation_uid, kind, status, updated_at FROM conversation_items WHERE kind='question' LIMIT 10"
).fetchall()
print(f"  Total: {len(rows)}")
for r in rows:
    print(f"  uid={r['conversation_uid'][:40]} status={r['status']} updated={r['updated_at'][:16] if r['updated_at'] else 'NULL'}")
