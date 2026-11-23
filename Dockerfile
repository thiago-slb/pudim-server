FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PUDIM_HOST=0.0.0.0 PUDIM_PORT=8097 PUDIM_DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY pudim_server ./pudim_server
RUN pip install --no-cache-dir . && useradd --system --uid 10001 pudim && mkdir /data && chown pudim /data
USER pudim
VOLUME ["/data"]
EXPOSE 8097
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8097/healthz', timeout=5)"
CMD ["pudim-server"]
