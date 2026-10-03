FROM debian:bookworm-slim

ARG CLI_VERSION=0.8.0
ARG CLI_SHA512=cf61c2688c45e1055d8add6221d9471a5a5b64bf3bcdb86460f5cb18414596cc4df3cdb6627c9097c94bec32a3c9915ada3211ef2ae5be33c46ebbc996ccaa28

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates curl gnupg pass libsecret-1-0 tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/proton-drive "https://proton.me/download/drive/cli/${CLI_VERSION}/linux-x64/proton-drive" \
 && echo "${CLI_SHA512}  /usr/local/bin/proton-drive" | sha512sum -c - \
 && chmod +x /usr/local/bin/proton-drive

ENV HOME=/state/home \
    PROTON_DRIVE_CACHE_DIR=/state/cli \
    PROTON_DRIVE_CREDENTIALS_STORE=pass \
    PROTON_DRIVE_LOG_LEVEL=INFO \
    MIGRATE_STATE=/state \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --chmod=0644 migrate.py index.html setup.sh /app/

CMD ["python3", "/app/migrate.py"]
