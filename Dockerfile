FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SENTINEL_PUBLIC_DEMO=1 SENTINEL_HOST=0.0.0.0 PORT=8001

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY adapters/ adapters/
COPY core/ core/
COPY data/ data/
COPY service/ service/
COPY static/ static/

USER 10001
EXPOSE 8001
CMD ["python", "app.py"]
