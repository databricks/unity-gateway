FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY setup.py /tmp/setup.py
RUN python3 /tmp/setup.py /app && rm /tmp/setup.py
