# syntax=docker/dockerfile:1
# Railway "digest-cron" service: sends the weekly schedule digest trigger to
# the api, then exits. Build context: the cron/digest/ folder (Railway Root
# Directory = /cron/digest). Set the Cron Schedule to "0 * * * 4,5,6" in the
# service settings.
FROM alpine:3.20

RUN apk add --no-cache curl ca-certificates tzdata

COPY send-digest.sh /usr/local/bin/send-digest.sh

# Strip Windows line endings (a "railway up" from a Windows PC uploads them),
# then make the script runnable.
RUN sed -i 's/\r$//' /usr/local/bin/send-digest.sh \
 && chmod +x /usr/local/bin/send-digest.sh

USER nobody
CMD ["/usr/local/bin/send-digest.sh"]
