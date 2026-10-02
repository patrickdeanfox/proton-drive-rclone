# migrate/ — one-time Proton Drive → NAS migration

Downloads only what the NAS does not already have, verifies every file by SHA1,
restores modification times and moves files to their destinations. Runs on the NAS
as a Docker stack with a one-page status UI. Never deletes anything, never changes
anything in Proton Drive.

## Pieces

| File | Role |
|---|---|
| `inventory.py` | Lists all of Proton Drive (metadata only) into `inventory.sqlite` using the official CLI. Resumable. |
| `manifest.py` | Joins the inventory with the NAS comparison (`nas-index.sqlite`, table `placement`) into `migration.sqlite`: per-file state, destination and download jobs. |
| `migrate.py` | The runner and UI. Reads `migration.sqlite`, runs CLI download jobs inside the time window, then verifies and places files. |
| `index.html` | The UI served by `migrate.py` on port 8104. |
| `Dockerfile`, `docker-compose.yml`, `setup.sh` | Container with the Proton Drive CLI 0.8.0 (checksum pinned), `pass` for the session store. |

## Destinations (decided 2026-10-02)

| Content | Destination |
|---|---|
| Images and videos (any folder except `data`) and the Photos timeline | `/volume1/Media/photos/proton-import/<proton path>` — then import into Immich |
| `data/data/...` | `/volume1/Media/adult-archive/<same relative path>` |
| Everything else | `/volume1/@home/PDFOX/Proton Drive/<proton path>` |

Staging while downloading: `/volume1/Media/.proton-staging/`. After placement it holds only
files that were already on the NAS (downloaded as part of a folder job) and a `_bad/` folder
with anything that failed verification. Safe to delete by hand once reviewed.

## Deploy on the NAS

```bash
# on the NAS
mkdir -p /volume3/docker/proton-migrate/state/home /volume3/docker/proton-migrate/state/cli
# copy this folder to /volume3/docker/proton-migrate/ and migration.sqlite to .../state/
cd /volume3/docker/proton-migrate
sudo docker compose build
sudo docker compose run --rm proton-migrate sh /app/setup.sh
sudo docker compose up -d
```

UI: http://192.168.4.42:8104 — "Sign in to Proton" (shows a link valid about 20 minutes; open it
and log in), Start / Pause, weekend window, retry failed jobs, log.

## After the download finishes

1. The runner verifies and places files on its own when the last job finishes (or press "Verify and place now").
2. Import the photos into Immich (Immich skips exact duplicates by checksum):
   ```bash
   sudo docker run --rm -it -v /volume1/Media/photos/proton-import:/import ghcr.io/immich-app/immich-cli:latest \
     upload --server http://192.168.4.42:2283 --key <immich-api-key> --recursive /import
   ```
3. Review `_bad/` and the failed jobs list, then remove the stack (`docker compose down`) and the staging folder.

## Known limits

- The CLI cannot run two downloads at once (its local cache crashes), so jobs run one at a time; each job downloads 5 files in parallel.
- A folder job re-run uses the CLI's `skip` strategy (by name), so verification is what catches partial files; they are moved to `_bad/` and the job can be retried.
- Live Photos: the paired video may not be exported by the CLI; check the Photos section result in Immich.
