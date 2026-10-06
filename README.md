# Daily Radio News + Lines

Cloud job (GitHub Actions) that runs at **7 AM UK every day**.

It pulls today's headlines from a spread of UK feeds (BBC News, BBC UK, BBC
London, BBC Entertainment, BBC Sport, Sky News UK, Sky Offbeat, Guardian UK),
asks an LLM to pick **five** stories and write, for each, a short conversational
summary plus 2-3 optional lines Chris could actually say on air, then DMs it to
him on Telegram and emails a copy.

- `post.py` - everything: fetch, draft, format, send.
- `.github/workflows/daily-news.yml` - schedule (06:00 + 07:00 UTC) + window gate.
- `state/last_sent.txt` - sent-today marker so the second fire cannot double-send.

Format lives in code; the model only writes the variable body, so the frame can
never drift. Nothing is invented - the model sees only the fetched headline text.

## Testing

```
ALLOW_ANY_HOUR=1 DRY_RUN=1 python3 post.py      # render locally, send nothing
python3 run_test.py                             # real send (DM + email)
gh workflow run "Daily Radio News + Lines" --repo CGTalent/daily-radio-news
```
