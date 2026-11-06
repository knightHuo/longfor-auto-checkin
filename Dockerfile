ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="Longfor Auto Check-in" \
    org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data \
    AUX_AUTH_FILE=/run/secrets/panel-auth.json \
    LONGHU_CAPTCHA=off

# 允许构建时指定 pip 源（国内可传清华源加速），默认官方源
ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app

COPY requirements.txt ./
RUN pip install -i ${PIP_INDEX_URL} -r requirements.txt

COPY app ./app

RUN groupadd --gid 10001 panel && useradd --uid 10001 --gid 10001 --no-create-home panel \
    && mkdir -p /data && chown 10001:10001 /data
VOLUME ["/data"]
USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
