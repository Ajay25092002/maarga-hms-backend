
FROM python:3.12-slim

# Prevent Python from buffering logs and generating bytecode files.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better Docker layer caching.
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Create a non-root application user.
RUN groupadd --system appgroup \
    && useradd --system --gid appgroup --create-home appuser

# Copy backend source code.
COPY . .

# Create a writable log directory.
RUN mkdir -p /app/logs \
    && chown -R appuser:appgroup /app

USER appuser

# FastAPI port.
EXPOSE 8001

# Start the application.
CMD ["python", "main.py"]
