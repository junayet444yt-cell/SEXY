FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV REQUIRE_PASSWORD=1
CMD gunicorn -b 0.0.0.0:${PORT:-10000} -w 1 --threads 8 --timeout 900 server:app
