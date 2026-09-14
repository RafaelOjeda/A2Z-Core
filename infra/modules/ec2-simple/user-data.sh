#!/bin/bash
set -e

# A2Z Core EC2 user data script -- single-box MVP
# (docs/architecture/single-box-mvp.md).
#
# Every brace-wrapped $NAME below (Terraform interpolation syntax -- not
# spelled out literally here, since that literal sequence is itself
# invalid HCL and breaks `terraform validate`'s templatefile() parser,
# comment or not) is a Terraform template variable (see main.tf's
# templatefile() call), substituted once at `terraform apply` time -- this
# script contains no bash-native variable expansion except where noted with
# a bare `$NAME` (no braces), which Terraform's templatefile leaves alone.
#
# Runs three containers via docker-compose, supervised as one unit so the
# existing "SSH in, journalctl -u a2z-core" troubleshooting story
# (DEPLOYMENT.md) still works for all three:
#   * postgres -- Omni-Channel + Invoicing's data layer. No RDS at MVP.
#   * app      -- the FastAPI monolith. RUN_OMNICHANNEL_WORKER=true here
#                 (nowhere else -- see Dockerfile/app/main.py's lifespan)
#                 since this is the one place the whole platform runs.
#   * caddy    -- TLS termination. Webhook providers (Meta, SES/SNS) require
#                 valid HTTPS; there is no ALB to do this for us anymore.

echo "=== A2Z Core EC2 Setup ==="
echo "Environment: ${environment}"
echo "Region: ${aws_region}"

apt-get update
apt-get upgrade -y

# --- Docker Engine + Compose plugin, from Docker's own apt repo -- stock
#     Ubuntu's `docker.io`/`docker-compose-plugin` packages either lag badly
#     or (on some Ubuntu releases) don't ship the compose plugin at all.
#     `unzip` is for the AWS CLI v2 installer just below. ---
apt-get install -y ca-certificates curl gnupg unzip
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker

# --- AWS CLI v2 -- not in apt; needed by the backup/restore scripts (`aws
#     s3 cp`) below. This instance is arm64 (aarch64), matching the t4g
#     instance type. ---
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install
rm -rf /tmp/awscliv2.zip /tmp/aws

mkdir -p /opt/a2z-core
cd /opt/a2z-core

# --- ECR auth via the credential helper, not a one-shot `docker login` --
#     a plain login token expires in 12h and this box never refreshes it,
#     so `docker compose pull` on a later deploy or restart would start
#     failing silently. The credential helper re-authenticates on every
#     pull instead, using the instance role (ecr:GetAuthorizationToken +
#     pull, granted in infra/modules/iam). ---
mkdir -p "$HOME/.docker"
curl -fsSL -o /usr/local/bin/docker-credential-ecr-login \
  https://amazon-ecr-credential-helper-releases.s3.us-east-1.amazonaws.com/latest/linux-arm64/docker-credential-ecr-login
chmod +x /usr/local/bin/docker-credential-ecr-login
ECR_REGISTRY=$(echo "${ecr_repository_url}" | cut -d/ -f1)
cat > "$HOME/.docker/config.json" <<DOCKERCFG
{"credHelpers": {"$ECR_REGISTRY": "ecr-login"}}
DOCKERCFG

# --- App environment. Only non-default values need setting -- everything
#     else falls back to app/config.py's defaults (table names, bucket,
#     event bus, queue names all already match). ---
cat > /opt/a2z-core/.env <<EOF
A2Z_ENV=${environment}
AWS_REGION=${aws_region}
DATABASE_URL=postgresql+asyncpg://a2z:${postgres_password}@postgres:5432/a2z
COGNITO_USER_POOL_ID=${cognito_user_pool_id}
COGNITO_APP_CLIENT_ID=${cognito_app_client_id}
COGNITO_REGION=${aws_region}
SES_NOTIFICATIONS_TOPIC_ARN=${ses_notifications_topic_arn}
RUN_OMNICHANNEL_WORKER=true
EOF
chmod 600 /opt/a2z-core/.env

# --- Caddyfile. $DOMAIN_NAME comes from the domain_name Terraform variable
#     (brace-wrapped ${domain_name} below, per the file header); $CADDY_SITE
#     is real bash, computed from it. Set domain_name (and point its DNS A
#     record at this instance's Elastic IP *first*, or the Let's Encrypt
#     HTTP-01 challenge will fail) to get automatic TLS; left blank (the
#     default), Caddy serves plain HTTP on :80 so the box is at least
#     reachable, and TLS is a manual follow-up before accepting real
#     webhook traffic. ---
DOMAIN_NAME="${domain_name}"
if [ -n "$DOMAIN_NAME" ]; then
  CADDY_SITE="$DOMAIN_NAME"
else
  CADDY_SITE=":80"
fi
cat > /opt/a2z-core/Caddyfile <<EOF
$CADDY_SITE {
    # flush_interval -1: disables response buffering. Needed for the
    # Omni-Channel SSE stream (GET /v1/omnichannel/orgs/{org_id}/stream,
    # app/routers/omnichannel.py) to actually reach clients as it's
    # written rather than sitting in Caddy's buffer -- harmless for every
    # other (ordinary, buffered-response) route.
    reverse_proxy app:8000 {
        flush_interval -1
    }
}
EOF

# --- docker-compose.yml: the three containers, one unit ---
cat > /opt/a2z-core/docker-compose.yml <<EOF
services:
  postgres:
    image: postgres:16-alpine
    restart: always
    environment:
      - POSTGRES_USER=a2z
      - POSTGRES_PASSWORD=${postgres_password}
      - POSTGRES_DB=a2z
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U a2z -d a2z"]
      interval: 5s
      timeout: 3s
      retries: 10

  app:
    image: ${ecr_repository_url}:${docker_image_tag}
    restart: always
    depends_on:
      postgres:
        condition: service_healthy
    env_file: /opt/a2z-core/.env
    expose:
      - "8000"

  caddy:
    image: caddy:2-alpine
    restart: always
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /opt/a2z-core/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - app

volumes:
  pgdata:
  caddy_data:
  caddy_config:
EOF

# --- systemd unit: one process (docker compose, foreground) to supervise ---
cat > /etc/systemd/system/a2z-core.service <<EOF
[Unit]
Description=A2Z Core (app + postgres + caddy via docker compose)
After=docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/opt/a2z-core
ExecStartPre=-/usr/bin/docker compose pull
ExecStart=/usr/bin/docker compose up
ExecStop=/usr/bin/docker compose down
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable a2z-core
systemctl start a2z-core

# --- Backup + restore config, shared by both scripts below via env vars
#     rather than Terraform substitution inside the scripts themselves --
#     that's what lets tests/integration/backup/test_restore_drill.py run
#     the identical scripts directly, overriding just PG_EXEC/S3_BUCKET. ---
cat > /opt/a2z-core/backup.env <<EOF
export S3_BUCKET=${s3_bucket}
export COMPOSE_FILE=/opt/a2z-core/docker-compose.yml
export PGUSER=a2z
export PGDATABASE=a2z
EOF
chmod 600 /opt/a2z-core/backup.env

# --- Nightly Postgres backup -> S3 (non-negotiable: no RDS safety net here,
#     app/services/omnichannel/CLAUDE.md §12). Reuses the ledger bucket's
#     existing IAM grant (s3:PutObject/GetObject/ListBucket on
#     arn:aws:s3:::<bucket>/*, already covers this prefix) rather than
#     needing a new one. NOTE: the ledger bucket's lifecycle rule
#     (infra/modules/s3) is tuned for invoice PDFs/attachments, not
#     backups specifically, and is NOT prefix-scoped -- it also governs
#     backups/postgres/ (Glacier at 60d, deleted at 90d). See
#     DEPLOYMENT.md's backup runbook for the usable restore window this
#     creates.
#
#     Script content comes from
#     infra/modules/ec2-simple/files/backup-postgres.sh via file() at
#     `terraform apply` time (see main.tf) -- read THAT file for the full
#     env-var contract; this heredoc only places it on the box. The
#     heredoc delimiter is quoted so bash does not expand the script's own
#     $VARS while writing it -- they're meant to be evaluated when the
#     script itself runs, not now. ---
cat > /opt/a2z-core/backup-postgres.sh <<'BACKUP_EOF'
${backup_script}
BACKUP_EOF
chmod +x /opt/a2z-core/backup-postgres.sh

# --- Restore companion, same sourcing story (files/restore-postgres.sh).
#     Not run automatically -- invoked by an operator during a restore
#     drill or a real incident. See DEPLOYMENT.md's backup runbook for the
#     full cutover procedure, including the Alembic-upgrade step this
#     script does not attempt to automate. ---
cat > /opt/a2z-core/restore-postgres.sh <<'RESTORE_EOF'
${restore_script}
RESTORE_EOF
chmod +x /opt/a2z-core/restore-postgres.sh

cat > /etc/cron.d/a2z-core-backup <<EOF
0 3 * * * root . /opt/a2z-core/backup.env && /opt/a2z-core/backup-postgres.sh >> /var/log/a2z-core-backup.log 2>&1
EOF
chmod 644 /etc/cron.d/a2z-core-backup

echo "=== A2Z Core EC2 Setup Complete ==="
