FROM python:3.13-slim
LABEL org.opencontainers.image.source="https://github.com/p1zzm1ssile/controlarr" \
      org.opencontainers.image.description="Stop and start Docker containers at set times of day" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY controlarr ./controlarr
ENV TZ=UTC PYTHONUNBUFFERED=1
VOLUME /config
EXPOSE 8080
# Checks the web page answers. With the page turned off (port 0) it always passes.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "-m", "controlarr.healthcheck"]
CMD ["python", "-m", "controlarr"]
