#!/usr/bin/env bash
# Build dist/lambda.zip — one artifact serving both out-of-band handlers:
#   app.lambdas.cognito_post_confirm.handler
#   app.lambdas.ses_notifications.handler
#
# The infra cognito module consumes this via `filename` + `source_code_hash`
# (see infra/modules/cognito). boto3/botocore are provided by the Lambda
# python3.12 runtime, so they are stripped from the bundle to keep it small.
set -euo pipefail

cd "$(dirname "$0")/.."
BUILD_DIR=build/lambda
DIST=dist/lambda.zip

rm -rf "$BUILD_DIR" "$DIST"
mkdir -p "$BUILD_DIR" dist

# Runtime deps only (no dev extras); target dir keeps the host env untouched.
python -m pip install --quiet --no-cache-dir --target "$BUILD_DIR" .

# Provided by the Lambda runtime — shipping them just bloats the zip.
rm -rf "$BUILD_DIR"/boto3 "$BUILD_DIR"/botocore "$BUILD_DIR"/*.dist-info/../boto3* 2>/dev/null || true
find "$BUILD_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

(cd "$BUILD_DIR" && zip -qr "../../$DIST" .)
echo "built $DIST ($(du -h "$DIST" | cut -f1))"
