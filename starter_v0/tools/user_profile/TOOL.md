---
name: user_profile
track: team_new
kind: live_api
provider: RapidAPI Twitter API45
requires_env: [RAPIDAPI_KEY, RAPIDAPI_TWITTER_HOST]
inputs: [screenname]
outputs: [items]
side_effect: false
---
# user_profile

Fetches profile stats for a single Twitter/X account: follower count, following
count, tweet count, and bio. `screenname` is an account handle without `@`.

Use this when the user asks about an account's stats or bio (followers,
following, verified, description) — not for the account's recent posts, which
is `timeline`.

## Quicktest

```bash
python -c "from pathlib import Path; from env_loader import load_lab_env; load_lab_env(Path.cwd()); from tools import TOOL_FUNCTIONS as T; r=T['user_profile']('sama'); items=r.get('items') or []; print({'error':r.get('error'), 'message':r.get('message'), 'item_count':len(items), 'first_title':items[0].get('title') if items else None})"
```
