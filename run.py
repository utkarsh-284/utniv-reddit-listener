"""Entry point. Local:  python run.py   |   CI: same, on a 30-min cron.

Flags:
  --dry-run   fetch + score + print to console, but do NOT write Supabase or post Slack
"""

import sys
from listener.pipeline import run

if __name__ == "__main__":
    if "--reset" in sys.argv:
        from listener import store
        store.reset(store.client())
        print("reset: cleared reddit_threads + reddit_runs (run `python run.py` next)")
        sys.exit(0)
    if "--realert" in sys.argv:
        from listener.pipeline import realert
        stats = realert()
        sys.exit(0 if stats.get("ok") else 1)
    if "--digest" in sys.argv:
        from listener.digest import weekly_digest
        stats = weekly_digest(dry="--dry-run" in sys.argv)
        sys.exit(0 if stats.get("ok") else 1)
    dry = "--dry-run" in sys.argv
    stats = run(dry=dry)
    sys.exit(0 if stats.get("ok") else 1)
