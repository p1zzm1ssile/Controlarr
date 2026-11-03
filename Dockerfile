FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY controlarr ./controlarr
ENV TZ=UTC PYTHONUNBUFFERED=1
VOLUME /config
CMD ["python", "-m", "controlarr"]
