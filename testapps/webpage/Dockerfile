FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/seaburr/picopaas"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY app.py .
COPY static ./static
USER 10001:10001
EXPOSE 8080
CMD ["python", "app.py"]
