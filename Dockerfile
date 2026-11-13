FROM python:3.11-slim AS backend

WORKDIR /app

# install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# install python deps
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# install node
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# install frontend deps and build
COPY frontend /app/frontend
RUN cd /app/frontend && npm install && npm run build

# app code
COPY backend /app/backend
COPY . /app

ENV PYTHONPATH=/app/backend
ENV PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "cd /app/backend && uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]