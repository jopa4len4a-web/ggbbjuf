FROM python:3.12-slim

# Код лежит вне /app: BotHost может монтировать /app поверх образа.
WORKDIR /usr/src/twaraga
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data PORT=3000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY app ./app
COPY static ./static
RUN mkdir -p /app/data && chmod 777 /app/data

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','3000')+'/health',timeout=4)"
CMD ["python", "/usr/src/twaraga/main.py"]
