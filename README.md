# Asian Money Scanner v0.4

Mobile dashboard and Telegram alert scaffold for validated accepted-bet records. This project does not include a verified live feed of accepted bets from Asian bookmakers. Public RSS observations are stored separately and are not treated as accepted bets. Missing or incomplete data must not produce market-share alerts.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Set persistent storage and Telegram settings through environment variables in Railway. Never commit secrets.

## Data integrity

Only records explicitly validated as accepted bets may qualify for stake alerts. Exchange matched volume, odds movements, bookmaker limits and estimated money flow are not accepted bets. Percentages require a known, complete market pool. No data means no alert.

See app/main.py and sample_record.json for the API contract.