FROM python:3.11-slim@sha256:ae52c5bef62a6bdd42cd1e8dffef86b9cd284bde9427da79839de7a4b983e7ca

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin xcapid \
    && install -d -o 10001 -g 10001 -m 0700 /run/axonos-x-capi \
    && install -d -o 10001 -g 10001 -m 0700 /run/axonos-x-capi-privacy \
    && pip install --no-cache-dir psycopg2-binary==2.9.10 cryptography==46.0.1

WORKDIR /app
COPY axonos_gate/x_capi.py axonos_gate/x_capi_worker.py axonos_gate/x_capi_cli.py /app/
COPY docker/x-capi-worker/init_privacy_fence.py /app/init_privacy_fence.py
COPY docker/x-capi-worker/bootstrap_x_capi_db.sh /app/bootstrap_x_capi_db.sh

USER 10001:10001
ENTRYPOINT ["python3", "/app/x_capi_worker.py"]
