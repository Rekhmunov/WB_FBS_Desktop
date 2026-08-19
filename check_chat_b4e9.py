import sys, os, subprocess
sys.path.insert(0, "/var/www/www-root/data/www/feedpilot")

env_out = subprocess.check_output(["systemctl", "show", "feedpilot", "--property=EnvironmentFiles"], text=True)
import re
env_file = re.search(r'EnvironmentFiles=(.*?) \(', env_out)
db_url = ""
if env_file:
    try:
        with open(env_file.group(1).strip()) as f:
            for line in f:
                if "APP_DB_URL" in line:
                    db_url = line.strip().split("=", 1)[1]
    except: pass

if not db_url:
    pid_out = subprocess.check_output(["systemctl", "show", "feedpilot", "--property=MainPID"], text=True)
    pid = pid_out.strip().split("=")[1]
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

# Check the chat
chat_uid = "1:wb:29:chat:1:b4e90879-6bf6-6dba-ff82-d68b8f558a15"
print(f"=== Chat {chat_uid[:50]}... ===")
row = conn.execute(
    "SELECT conversation_uid, kind, status, last_sent_at, last_message_at, updated_at, customer_name FROM conversation_items WHERE conversation_uid = %s",
    (chat_uid,)
).fetchone()
if row:
    for k, v in row.items():
        print(f"  {k}: {v}")
else:
    print("  NOT FOUND")

# Check messages in DB for this chat
print("\n=== Messages in DB ===")
msgs = conn.execute(
    "SELECT direction, message_text, created_at, send_status FROM conversation_messages WHERE conversation_uid = %s ORDER BY created_at ASC",
    (chat_uid,)
).fetchall()
print(f"  Total messages: {len(msgs)}")
for m in msgs:
    text = str(m['message_text'] or '')[:50]
    print(f"  [{m['direction']}] {m['created_at']} status={m['send_status']} text={text}")

# Also check the other Dmitry
chat_uid2 = "1:wb:29:chat:1:d6be87eb-312a-f656-d0a9-ecca6ffd8af2"
print(f"\n=== Other Dmitry {chat_uid2[:50]}... ===")
row2 = conn.execute(
    "SELECT last_sent_at, last_message_at, status FROM conversation_items WHERE conversation_uid = %s",
    (chat_uid2,)
).fetchone()
if row2:
    print(f"  last_sent_at: {row2['last_sent_at']}")
    print(f"  last_message_at: {row2['last_message_at']}")
    print(f"  status: {row2['status']}")
