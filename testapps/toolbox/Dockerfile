FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/seaburr/picopaas"
ENV PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends nmap curl dnsutils mariadb-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
USER 10001:10001
EXPOSE 8080
CMD ["python", "app.py"]
