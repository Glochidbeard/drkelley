# Dr. Kelley

Treatment plan builder for the nursery. Afflictions go on the left, reusable
treatment templates on the right; apply templates to an affliction to build
its ordered treatment plan. Exports two CSVs for Viridian.

## Exports

**`/export/plans.csv`** — one row per affliction:

| Affliction | Treatment 1 | Frequency 1 | Treatment 2 | Wait 1-2 | Frequency 2 | Treatment 3 | Wait 2-3 | Frequency 3 | … |

Treatment cells hold the template name (unique), which joins to the templates CSV.

**`/export/templates.csv`** — one row per template:

| Template | Method | Chemical | Rate | Week of application (`2026-W40`) | REI (hours) |

## Data

- `chemicals` is seeded from `data/rei_list.csv` on startup (new names only —
  REI edits made on the Pharmacy page are never overwritten).
- Tables are created automatically on boot; no migrations.

## Run locally

```
pip install -r requirements.txt
python app.py
```

Without `DATABASE_URL` it uses a local SQLite file (`drkelley.db`).

## Deploy (Railway)

1. Push this repo to GitHub and create a Railway service from it.
2. Add a Postgres database to the project and reference its `DATABASE_URL`
   on the service.
3. Set `SECRET_KEY` to any random string.

The `Procfile` runs gunicorn with `--preload` so the schema/seed step runs once.
