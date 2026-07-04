FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir . && pip install --no-cache-dir supervisor

FROM python:3.12-slim
WORKDIR /app
RUN useradd -m -u 1000 ecl
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts
COPY docker/supervisord.conf ./supervisord.conf
RUN mkdir -p run \
    && chown -R ecl:ecl /app \
    && chmod +x scripts/docker_entrypoint.sh
USER ecl
EXPOSE 8000
CMD ["bash", "scripts/docker_entrypoint.sh"]
