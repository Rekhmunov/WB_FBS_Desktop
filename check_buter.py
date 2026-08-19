import sys, subprocess, datetime
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
uid = "1:wb:29:chat:1:b4e90879-6bf6-6dba-ff82-d68b8f558a15"

print("=== conversation_items ===")
row = conn.execute("SELECT last_sent_at, last_message_at, unread_count FROM conversation_items WHERE conversation_uid = %s", (uid,)).fetchone()
if row:
    print(f"  last_sent_at:    {row['last_sent_at']}")
    print(f"  last_message_at: {row['last_message_at']}")
    print(f"  unread_count:    {row['unread_count']}")

print("\n=== All inbound messages (direction=inbound) ===")
msgs = conn.execute(
    "SELECT direction, message_text, created_at FROM conversation_messages WHERE conversation_uid = %s AND direction='inbound' ORDER BY created_at DESC LIMIT 10",
    (uid,)
).fetchall()
print(f"  Total inbound: {len(msgs)}")
for m in msgs:
    ct = str(m['created_at'])
    print(f"  [{m['direction']}] created={ct[:25] if ct else 'NULL/EMPTY'} | {str(m['message_text'])[:40]}")

print("\n=== Batch fix check ===")
# What does the batch SQL find?
result = conn.execute("""
    SELECT ci.conversation_uid, ci.last_sent_at, 
           (SELECT MAX(cm.created_at::text) FROM conversation_messages cm 
            WHERE cm.conversation_uid = ci.conversation_uid 
              AND cm.direction = 'inbound'
              AND cm.created_at IS NOT NULL
              AND cm.created_at::text != '') as newest_inbound
    FROM conversation_items ci
    WHERE ci.conversation_uid = %s
""", (uid,)).fetchone()
if result:
    print(f"  last_sent_at:    {result['last_sent_at']}")
    print(f"  newest_inbound:  {result['newest_inbound']}")
    if result['last_sent_at'] and result['newest_inbound']:
        lsa = str(result['last_sent_at'])
        ni = str(result['newest_inbound'])
        print(f"  Would move? newest_inbound > last_sent_at: {ni > lsa}")
    else:
        print("  Cannot compare (one is None/empty)")
