# extractor

Copies a legacy database from a VPS (through an SSH tunnel, read-only) into a local
SQLite file, then finds the image references in it, downloads the images, and links
each reference to its local file. Results are also written as CSV and JSONL per table.

```
probe -> dump -> images scan -> images download -> link -> export
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp config.example.yaml config.yaml     # then edit
cp .env.example .env                   # then fill in the two passwords
```

`config.yaml` and `.env` are git-ignored. Passwords are read only from `.env` / environment
(`EXTRACTOR_SSH_PASSWORD`, `EXTRACTOR_DB_PASSWORD`), never from the YAML.

## Usage

```bash
# 1. (optional) Look around the VPS. Give it a filename taken from the DB to find the image folder.
extractor probe -c config.yaml --find-file 20220324111256_83rxz.jpg
#    -> put the printed folder in images.sources[].sftp_dir (and/or a public base_url)

# 2. Try one small table first
extractor dump -c config.yaml --table rumah_foto

# 3. Preview which columns are detected as images (writes nothing with --dry-run)
extractor images scan -c config.yaml --dry-run

# 4. Everything
extractor run -c config.yaml
```

Single steps: `dump`, `images scan`, `images download [--retry-failed]`, `link`, `export`.
Every step is re-runnable. `dump` skips tables already finished (`--fresh` to redo),
`images download` skips files already downloaded.

## Image sources

The DB stores bare filenames (e.g. `20220324111256_83rxz.jpg`), so `images.sources` says where
they live. For each reference, sources whose `columns` patterns match are tried in order:
`base_url` over HTTP first, then `sftp_dir` straight from the VPS disk. Full URLs in the DB are
downloaded as-is. Columns are auto-detected (hit ratio >= `min_hit_ratio`); use
`include_columns` / `exclude_columns` to override.

## Output (`output/`)

| Path | Content |
|---|---|
| `db.sqlite` | Full local copy. Added: `_image_refs`, `_images` (download manifest), `_image_links`, and `<column>_local` columns |
| `images/<source>/...` | Downloaded files |
| `csv/<table>.csv`, `jsonl/<table>.jsonl` | One file per table (binary values as base64) |
| `schema`-level notes | Types are simplified: ints/floats native, decimals/dates as text, binary as BLOB; foreign keys and secondary indexes are not recreated |

Original values are never changed. A `<column>_local` column is added only where each row has a
single image; multi-image columns (e.g. JSON galleries) are covered by `_image_links`.

## Safety

- The remote DB connection is set to read-only; only `SELECT` is issued.
- Use a dedicated read-only DB user if you can, and rotate any password that was shared in plain text.
- Image downloads are limited to `images.concurrency` (default 8) so the old site isn't overloaded.

## Tests

```bash
pytest
```

Tests use a local fake database and a local image server; they need no VPS.
