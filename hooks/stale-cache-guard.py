#!/usr/bin/env python3
"""UserPromptSubmit hook: warn once before resuming a big session whose prompt cache expired.

Resuming after >1h re-writes the whole context at the 1h cache-write price
(Opus 5.5: $8/MTok). At 300K tokens that is ~$2.40 for one prompt. Over Sep 29-Oct 27
these rebuilds cost $689 (25% of spend). The hook blocks the first prompt and
explains; sending the same prompt again goes through.
"""
import json, os, sys, time, datetime as dt

IDLE_S = int(os.environ.get('STALE_CACHE_IDLE_S', 3300))      # 55 min: 1h TTL minus margin
MIN_CTX = int(os.environ.get('STALE_CACHE_MIN_CTX', 120000))
WRITE_PER_MTOK = {'fable': 20.0, 'opus-5-5': 8.0, 'opus': 10.0, 'sonnet': 4.0, 'haiku-5': 0.2}

def main():
    h = json.load(sys.stdin)
    path = h.get('transcript_path') or ''
    if not os.path.exists(path): return 0
    last = None
    with open(path, 'rb') as f:                 # scan only the tail
        f.seek(0, 2); f.seek(max(0, f.tell() - 2_000_000))
        for line in f:
            if b'"usage"' not in line: continue
            try: d = json.loads(line)
            except Exception: continue
            if d.get('type') == 'assistant' and (d.get('message') or {}).get('usage'): last = d
    if not last: return 0
    u = last['message']['usage']
    ctx = sum(u.get(k, 0) or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
    idle = time.time() - dt.datetime.fromisoformat(last['timestamp'].replace('Z', '+00:00')).timestamp()
    if idle < IDLE_S or ctx < MIN_CTX: return 0
    marker = f"/tmp/stale-cache-ack-{h.get('session_id')}-{last['timestamp']}"
    if os.path.exists(marker): return 0
    open(marker, 'w').close()
    model = (last['message'].get('model') or '')
    rate = next((v for k, v in WRITE_PER_MTOK.items() if k in model), 8.0)
    print(f"Prompt cache expired ({idle/3600:.1f}h idle). This prompt re-writes {ctx/1000:.0f}K tokens "
          f"≈ ${ctx*rate/1e6:.2f} before any work, and every later turn re-reads them.\n"
          f"Cheaper: if the task state lives in a file (status.md, plan, PR), /clear and point the new "
          f"context at it (≈90K tokens). "
          f"Send the same prompt again to continue anyway.", file=sys.stderr)
    return 2

if __name__ == '__main__':
    sys.exit(main())
