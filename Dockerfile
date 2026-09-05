FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY . .
RUN python -m pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "retail.api.main:app", "--app-dir", "/app/examples", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
