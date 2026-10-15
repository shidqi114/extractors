# extractor

Copies a legacy database from a VPS (through an SSH tunnel, read-only) into a local
SQLite file, then finds the image references in it, downloads the images, and links
each reference to its local file. Results are also written as CSV and JSONL per table.

```
probe -> dump -> images scan -> images download -> link -> export -> organize
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

Single steps: `dump`, `images scan`, `images download [--retry-failed]`, `link`, `export`,
`organize [--hardlink]`.
Every step is re-runnable. `dump` skips tables already finished (`--fresh` to redo),
`images download` skips files already downloaded.

## Image sources

The DB stores bare filenames (e.g. `20220324111256_83rxz.jpg`), so `images.sources` says where
they live. For each reference, sources whose `columns` patterns match are tried in order:
`base_url` over HTTP first, then `sftp_dir` straight from the VPS disk. Full URLs in the DB are
downloaded as-is. Columns are auto-detected (hit ratio >= `min_hit_ratio`); use
`include_columns` / `exclude_columns` to override.

## When images don't download

`extractor images download` prints how many images are on disk and a few example failures. If none
downloaded, the `base_url` / `sftp_dir` are wrong. Test one image without editing the config:

```bash
extractor images check --base-url https://your-site.com/path/to/photos/
extractor images check --sftp-dir /var/www/site/photos
```

`check` shows, for each place it tries, whether the file was found (HTTP status, file type, size, or the
SFTP error). Once it says FOUND, put those values in `images.sources` and run `extractor images download`
again. Images that failed under the old settings are retried automatically when the sources change;
`--retry-failed` retries them with unchanged settings.

## Houses and agents folders (`organize`)

After the images are downloaded and linked, `extractor organize` (also the last step of `run`) builds:

```
output/
  houses/
    <id_rumah>/
      house.json              # the rumah row + main photo + photo list + link to the agent
      photos/
        main_<filename>.jpg   # rumah.foto
        <id_foto>_<filename>.jpg   # one per rumah_foto row
  agents/
    <agent name>/
      agent.json              # the agent row + the ids of the houses they list
```

- `house.json` has `house` (all columns of the rumah row), `main_photo`, `photos` (each with its
  rumah_foto row and the file/status of its image) and `agent` (id plus a relative path to `agent.json`).
  Photos that could not be downloaded are listed with their status (`not_found`, `error`, ...), not dropped.
- Houses link to agents through `rumah.agent`; every row of the agent table gets a folder named after the
  agent, with the list of houses they list in `agent.json`. Two agents with the same name get their id
  added (`Widodo_4`, `Widodo_9`); an agent without a name becomes `agent_<id>`. The name column is
  auto-detected (`nama_agent`, `nama`, `name`, ...); set `organize.agent_name` to choose another.
- Re-running only adds and overwrites. If you switch the folder naming, delete `output/agents` first so old folders don't stay behind.
- Photos are copied out of `images/`, which stays as the download cache. `--hardlink` saves disk space.
- Column names default to `id_rumah`, `id_foto`, `rumah.foto`, `rumah.agent` and the agent table's primary key.
  If one is wrong, the command says which and lists the real columns; fix it in the `organize:` section of `config.yaml`.

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
