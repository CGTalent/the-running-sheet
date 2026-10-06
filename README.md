# The Running Sheet

Chris's daily radio brief. Runs in the cloud (GitHub Actions) at **7 AM UK every
day** - so it arrives whether or not his Mac is on.

It pulls today's headlines from a spread of UK feeds (BBC News, BBC UK, BBC
London, BBC Politics, BBC Entertainment, BBC Sport, Sky UK, Sky Strange, Sky
Entertainment, Guardian UK, Guardian Sport), asks an LLM to pick **five**
stories with a spread of topics, writes a short conversational summary for each
plus 2-3 **optional lines Chris could actually say on air**, then delivers it as
a Telegram DM **and** an emailed copy.

- `post.py` - everything: fetch, draft, format, send.
- `.github/workflows/running-sheet.yml` - schedule (06:00 + 07:00 UTC) + window gate.
- `state/last_sent.txt` - sent-today marker so the second fire cannot double-send.

The frame/header lives in code; the model only writes the variable body, so the
format can never drift. Nothing is invented - the model sees only the fetched
headline text and is told to use nothing else.

Delivery: Telegram DM to Chris + email to chrisfarrell2012@gmail.com.

## Testing

```
ALLOW_ANY_HOUR=1 DRY_RUN=1 python3 post.py      # render locally, send nothing
python3 run_test.py                             # real send (DM + email)
gh workflow run "The Running Sheet" --repo CGTalent/the-running-sheet
```
