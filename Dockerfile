FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV MODE=serve
ENV CACHE_DIR=/data
RUN mkdir -p /data

CMD ["python", "-u", "bot.py"]
