"""A command hook for tests: reads the payload as JSON on stdin and answers on stdout.

Usage: hook_command.py <mode> [log file]
- deny-secret: deny source_read of secret.md, allow everything else
- sleep: never answers (for the timeout test)
- fail: exit with status 3
- env: deny with the reason 'visible' or 'hidden' depending on HOOK_SECRET
- log: append each payload to the log file (after_tool)
"""
import json
import os
import sys
import time

payload = json.load(sys.stdin)
mode = sys.argv[1]
if mode == "sleep":
    time.sleep(60)
elif mode == "fail":
    sys.exit(3)
elif mode == "env":
    print(json.dumps({"decision": "deny", "reason": "visible" if os.environ.get("HOOK_SECRET") else "hidden"}))
elif mode == "log":
    with open(sys.argv[2], "a", encoding="utf-8") as log:
        log.write(json.dumps(payload) + "\n")
elif payload["arguments"].get("path") == "secret.md":
    print(json.dumps({"decision": "deny", "reason": "secret.md is off limits for reports."}))
else:
    print(json.dumps({"decision": "allow"}))
