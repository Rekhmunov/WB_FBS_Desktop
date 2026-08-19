import sys, subprocess
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

uid = "1:wb:29:chat:1:b4e90879-6bf6-6dba-ff82-d68b8f558a15"

print("=== conversation_items ===")
row = conn.execute(
    "SELECT last_sent_at, last_message_at, unread_count FROM conversation_items WHERE conversation_uid = %s",
    (uid,)
).fetchone()
if row:
    print(f"  last_sent_at:    {row['last_sent_at']}")
    print(f"  last_message_at: {row['last_message_at']}")
    print(f"  unread_count:    {row['unread_count']}")

print("\n=== Last 5 messages (newest first) ===")
msgs = conn.execute(
    "SELECT direction, message_text, created_at FROM conversation_messages WHERE conversation_uid = %s ORDER BY created_at DESC LIMIT 5",
    (uid,)
).fetchall()
for m in msgs:
    print(f"  [{m['direction']}] {m['created_at'][:19] if m['created_at'] else 'NO_DATE'} | {str(m['message_text'])[:40]}")
