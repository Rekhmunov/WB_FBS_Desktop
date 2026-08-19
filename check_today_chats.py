import sys, subprocess
sys.path.insert(0, "/var/www/www-root/data/www/feedpilot")

pid_out = subprocess.check_output(["systemctl", "show", "feedpilot", "--property=MainPID"], text=True)
pid = pid_out.strip().split("=")[1]
db_url = ""
try:
    with open(f"/proc/{pid}/environ", "rb") as f:
        for item in f.read().split(b"\x00"):
            if b"APP_DB_URL" in item:
                db_url = item.decode().split("=", 1)[1]
except: pass

if not db_url:
    print("No DB URL"); sys.exit(1)

import psycopg, psycopg.rows
conn = psycopg.connect(db_url, row_factory=psycopg.rows.dict_row, autocommit=True)

print("=== Chats updated TODAY (2026-05-10) ===")
rows = conn.execute("""
    SELECT conversation_uid, customer_name, last_sent_at, last_message_at, 
           updated_at, unread_count,
           CASE WHEN last_sent_at IS NOT NULL AND (last_message_at IS NULL OR last_sent_at::text >= last_message_at::text)
                THEN 'Answered' ELSE 'New' END as bucket
    FROM conversation_items
    WHERE kind = 'chat' AND updated_at::date = '2026-05-10'
    ORDER BY updated_at DESC
    LIMIT 10
""").fetchall()
print(f"Count: {len(rows)}")
for r in rows:
    print(f"  [{r['bucket']}] name={r['customer_name']} uid={r['conversation_uid'][:40]}")
    print(f"    last_sent_at={str(r['last_sent_at'])[:25]} last_msg_at={str(r['last_message_at'])[:25]}")
    print(f"    unread={r['unread_count']} updated={str(r['updated_at'])[:19]}")

print("\n=== Total chats by bucket (all) ===")
rows2 = conn.execute("""
    SELECT 
        CASE WHEN last_sent_at IS NOT NULL AND (last_message_at IS NULL OR last_sent_at::text >= last_message_at::text)
             THEN 'Answered' ELSE 'New' END as bucket,
        COUNT(*) as cnt
    FROM conversation_items WHERE kind='chat'
    GROUP BY bucket
""").fetchall()
for r in rows2:
    print(f"  {r['bucket']}: {r['cnt']}")
