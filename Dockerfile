FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Paris
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY app/requirements.txt app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt
COPY app app
# /data and /bootstrap belong to the application user: a named volume mounted there inherits that ownership, with no initialization step.
# Group 10001: that of the built-in runner's token file; the runner receives it as an additional group (compose).
RUN groupadd -g 10001 fabrique && useradd -u 10001 -g 10001 -d /data fabrique && mkdir -p /data /bootstrap \
 && chown fabrique:fabrique /data /bootstrap && chmod 0750 /bootstrap
USER fabrique
ENV FABRIQUE_DATA=/data
ARG GIT_SHA=dev
ENV FABRIQUE_VERSION=$GIT_SHA RUNNER_VERSION=$GIT_SHA
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
