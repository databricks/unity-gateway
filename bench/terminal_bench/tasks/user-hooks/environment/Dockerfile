FROM python:3.12-slim
WORKDIR /app
COPY app/ /app/
COPY setup.py /tmp/setup.py
RUN python3 /tmp/setup.py /app && rm /tmp/setup.py
