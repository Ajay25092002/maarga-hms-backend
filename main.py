
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from Router.Login import router as login_router
from Router.Patient import router as patient_router
from Router.OPBilling import router as op_billing_router
from Router.Payments import router as payments_router
from Router.role_router import router as role_router
from Router.user_router import router as user_router
from Router.dashboard_router import router as dashboard_router
from contextlib import asynccontextmanager
from Core.Database import initialize_database

# Load environment variables from .env
load_dotenv(override=False)

# Application configuration
APP_NAME = os.getenv("APP_NAME", "Maarga Mindcare HMS API")
APP_ENV = os.getenv("APP_ENV", "development")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
RELOAD = os.getenv("RELOAD", "true").lower() == "true"


CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://16.176.31.53:5173"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs before the application starts accepting requests.
    initialize_database()
    yield

# Initialize FastAPI
app = FastAPI(
    title=APP_NAME,
    version="1.0.0",
    lifespan=lifespan,

)
app.include_router(login_router)
app.include_router(patient_router)
app.include_router(op_billing_router)
app.include_router(payments_router)
app.include_router(role_router)
app.include_router(user_router)
app.include_router(dashboard_router)

# Configure CORS
origins = [
    origin.strip()
    for origin in CORS_ORIGINS.split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "message": "Welcome to Maarga Mindcare HMS API",
        "environment": APP_ENV,
        "status": "running",
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        reload=RELOAD,
    )

