# Tiny test image for the job runner. Build + load into kind with
# `task build-job-test-image`, then run a job with image `local/job-test:latest`
# and command e.g. `/startup.sh hello`.
FROM alpine:3.19

COPY startup.sh /startup.sh
RUN chmod +x /startup.sh

# Convenience for `docker run`; the job runner overrides this via `sh -c`.
ENTRYPOINT ["/startup.sh"]
