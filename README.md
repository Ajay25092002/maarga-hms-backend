# Maarga Mindcare HMS — Backend

## Overview

The Maarga Mindcare Hospital Management System backend is built with **FastAPI**. It provides authentication APIs, email OTP verification, and Redis-backed login sessions.

## Prerequisites

- Docker installed and running.
- A configured environment file.
- `Dockerfile` and `requirements.txt` in the backend directory.
- Redis running in a Docker container.

# 1. Development Environment Setup

## Create Docker Network

```bash
docker network create maarga-hms-dev-network
```

## Create Redis Container

```bash
docker run -d \
  --name maarga-hms-redis-dev \
  --restart unless-stopped \
  --network maarga-hms-dev-network \
  -v maarga_hms_redis_dev_data:/data \
  redis:7-alpine \
  redis-server --appendonly yes
```

## Configure Environment

Create `.env.docker.dev` in the backend directory and configure:

```env
REDIS_URL=redis://maarga-hms-redis-dev:6379/0
```

Add the other environment variables required by your application.

## Build Docker Image

Run from the backend directory:

```bash
docker build -t maarga-hms-backend .
```

## Run the Backend Container

```bash
docker run -d \
  --name maarga-hms-backend-dev \
  --restart unless-stopped \
  --network maarga-hms-dev-network \
  -p 8002:8001 \
  --env-file .env.docker.dev \
  maarga-hms-backend
```

## Access Development API

- API base URL: `http://localhost:8002`
- Swagger documentation: `http://localhost:8002/docs`

# 2. Production Environment Setup

## Create Docker Network

```bash
docker network create maarga-hms-network
```

## Create Redis Container

```bash
docker run -d \
  --name maarga-hms-redis \
  --restart unless-stopped \
  --network maarga-hms-network \
  -v redis_data:/data \
  redis:7-alpine \
  redis-server --appendonly yes
```

Redis is accessible only to containers connected to the production Docker network. No public Redis port is required.

## Configure Environment

Create `.env.docker` in the backend directory and configure:

```env
REDIS_URL=redis://maarga-hms-redis:6379/0
```

Add the other environment variables required by your application, including production secrets and email/OTP settings.

## Build Docker Image

Run from the backend directory:

```bash
docker build -t maarga-hms-backend .
```

## Run the Backend Container

```bash
docker run -d \
  --name maarga-hms-backend \
  --restart unless-stopped \
  --network maarga-hms-network \
  -p 8001:8001 \
  --env-file .env.docker \
  maarga-hms-backend
```

The FastAPI application must listen on `0.0.0.0:8001` inside the container.

## Access Production API

For the production server at `164.52.213.96`:

- API base URL: `http://164.52.213.96:8001`
- Swagger documentation: `http://164.52.213.96:8001/docs`

# 3. Useful Docker Commands

## View Running Containers

```bash
docker ps
```

## View Backend Logs

Development:

```bash
docker logs -f maarga-hms-backend-dev
```

Production:

```bash
docker logs -f maarga-hms-backend
```

## Check Redis

Development:

```bash
docker exec maarga-hms-redis-dev redis-cli ping
```

Production:

```bash
docker exec maarga-hms-redis redis-cli ping
```

Expected response:

```text
PONG
```

## Restart Backend

Development:

```bash
docker restart maarga-hms-backend-dev
```

Production:

```bash
docker restart maarga-hms-backend
```

## Stop Backend

```bash
docker stop maarga-hms-backend
```

## Remove Backend Container

```bash
docker rm -f maarga-hms-backend
```

Removing a container does not remove its Docker image or named Redis volume.

# 4. Rebuild After Code Changes

Run from the backend directory.

Build the updated image:

```bash
docker build -t maarga-hms-backend .
```

Remove the existing production backend container:

```bash
docker rm -f maarga-hms-backend
```

Run the updated backend:

```bash
docker run -d \
  --name maarga-hms-backend \
  --restart unless-stopped \
  --network maarga-hms-network \
  -p 8001:8001 \
  --env-file .env.docker \
  maarga-hms-backend
```

Redis does not need to be recreated for normal backend code changes.

**Important:** Do not delete the `redis_data` volume during routine deployments. It contains persistent Redis data.

## Production Recommendations

- Keep Redis private and do not publish port `6379`.
- Use separate environment files for development and production.
- Do not commit secrets to Git.
- Configure HTTPS for production access.
- Restrict publicly accessible ports using server and provider firewall rules.
- Back up important Redis data and test the recovery process.
