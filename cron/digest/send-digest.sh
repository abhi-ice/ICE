#!/bin/sh
# Weekly schedule digest trigger for the Railway "digest-cron" service.
#
# Railway starts this on the schedule "0 * * * 4,5,6" (every hour, Thursday to
# Saturday, UTC). It sends ONE request to the api and exits. The api decides
# whether it is Friday 7 to 11:59 AM in each project's own time zone, and it
# ignores a repeat of the same X-Webhook-Id, so an extra run is harmless.
#
# This replaces Emergent's .emergent/cron/dispatch_webhook.sh and sends the
# same headers and body.
#
# Variables (set on this service in Railway):
#   API_URL              base address of the api, no trailing slash, for
#                        example http://api.railway.internal:8000
#   WEBHOOK_CRON_SECRET  must match the api's value, for example the reference
#                        variable ${{api.WEBHOOK_CRON_SECRET}}
set -eu

: "${API_URL:?API_URL is not set}"
: "${WEBHOOK_CRON_SECRET:?WEBHOOK_CRON_SECRET is not set}"

CRON_NAME="schedule-digest"
ENDPOINT="${API_URL%/}/api/cron/schedule-digest"

# The run id is the repeat guard on the api: the cron name plus the UTC date
# and hour, so two starts in the same hour count as one run.
RUN_ID="${CRON_NAME}-$(date -u +%Y%m%dT%H)00"
DISPATCH_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BODY="{\"event\":\"schedule.triggered\",\"schedule_id\":\"$CRON_NAME\",\"run_id\":\"$RUN_ID\",\"dispatch_time\":\"$DISPATCH_TIME\",\"job_id\":\"\",\"data\":null}"

# One request with a few quick retries (the private network can take a
# moment at start-up; the api skips repeats of the same X-Webhook-Id). The secret is passed
# in a header only and is never printed.
HTTP_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' \
	--max-time 30 \
	--retry 3 --retry-delay 2 --retry-connrefused --retry-all-errors \
	-X POST \
	-H "Authorization: Bearer $WEBHOOK_CRON_SECRET" \
	-H "Content-Type: application/json" \
	-H "X-Webhook-Id: $RUN_ID" \
	-H "X-Webhook-Timestamp: $DISPATCH_TIME" \
	-d "$BODY" \
	"$ENDPOINT" || true)"

echo "schedule digest trigger finished (run=$RUN_ID http=${HTTP_STATUS:-000})"

# Exit with an error on anything but a 2xx answer, so a failed run shows up
# as failed in Railway's cron history. Railway does not retry it.
case "$HTTP_STATUS" in
	2??) exit 0 ;;
	*) exit 1 ;;
esac
