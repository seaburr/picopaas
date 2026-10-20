FROM python:3.13-slim
LABEL org.opencontainers.image.source="https://github.com/seaburr/picopaas"
ENV PYTHONUNBUFFERED=1
# UID 10001 has no passwd entry; Python 3.13 getpass.getuser() raises OSError (pymysql calls it at import).
ENV HOME=/tmp USER=app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
USER 10001:10001
EXPOSE 8080
CMD ["python", "app.py"]
