# Maarga Mindcare HMS — Backend

## Overview
The Maarga Mindcare Hospital Management System backend is built with **FastAPI**. It provides authentication APIs, email OTP verification, and Redis-backed login sessions.

## Prerequisites
- Docker installed and running.
- A configured `.env` file in the backend directory.
- Redis running and accessible to the backend container.
- `Dockerfile` and `requirements.txt` in the backend directory.

## Build Docker Image

Run from the backend directory:

```powershell
docker build -t maarga-hms-backend .
```

## Run the Container

```powershell
docker run -d --name maarga-hms-backend -p 8001:8001 --env-file .env.docker maarga-hms-backend
```

The container starts the FastAPI application on port `8001`.

**Note:** If Redis runs on your host machine, configure `REDIS_URL` to use `redis://host.docker.internal:6379/0` and ensure the container can reach it. If Redis runs in another container, connect both containers to the same Docker network and use the Redis container's service name.

## Access the API

- API base URL: `http://localhost:8001`
- Swagger documentation: `http://localhost:8001/docs`

## Useful Docker Commands

View logs:

```powershell
docker logs -f maarga-hms-backend
```

Stop the container:

```powershell
docker stop maarga-hms-backend
```

Remove the container:

```powershell
docker rm maarga-hms-backend
```

Rebuild after code changes by rebuilding the image and recreating the container.