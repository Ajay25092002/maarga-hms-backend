import os
import psycopg2
from psycopg2 import errors
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from Router.Login import get_current_user

router = APIRouter(prefix="/api/sponsors", tags=["Sponsors"])

def db():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise HTTPException(status_code=500, detail="Database configuration is missing.")
    return psycopg2.connect(url, options="-c timezone=Asia/Kolkata")

class SponsorCreate(BaseModel):
    sponsor_name: str = Field(min_length=1, max_length=255)

class SponsorUpdate(BaseModel):
    sponsor_name: str = Field(min_length=1, max_length=255)

class SponsorStatus(BaseModel):
    is_active: bool

@router.get("")
def list_sponsors(search: str | None = Query(None, max_length=255), is_active: bool | None = None, current_user: dict = Depends(get_current_user)):
    connection = db()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, sponsor_name, is_active, created_at, updated_at FROM public.sponsors WHERE (%s IS NULL OR is_active = %s) AND (%s IS NULL OR sponsor_name ILIKE %s) ORDER BY CASE WHEN LOWER(sponsor_name) = 'cash' THEN 0 ELSE 1 END, sponsor_name", (is_active, is_active, search, f"%{search}%" if search else None))
            return [dict(row) for row in cur.fetchall()]
    finally:
        connection.close()

@router.post("", status_code=201)
def create_sponsor(payload: SponsorCreate, current_user: dict = Depends(get_current_user)):
    name = payload.sponsor_name.strip()
    if not name: raise HTTPException(status_code=422, detail="Sponsor name cannot be empty.")
    connection = db()
    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("INSERT INTO public.sponsors (sponsor_name) VALUES (%s) RETURNING id, sponsor_name, is_active, created_at, updated_at", (name,))
                return dict(cur.fetchone())
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="A sponsor with this name already exists.")
    finally: connection.close()

@router.put("/{sponsor_id}")
def update_sponsor(sponsor_id: int, payload: SponsorUpdate, current_user: dict = Depends(get_current_user)):
    name = payload.sponsor_name.strip()
    if not name: raise HTTPException(status_code=422, detail="Sponsor name cannot be empty.")
    connection = db()
    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("UPDATE public.sponsors SET sponsor_name=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id, sponsor_name, is_active, created_at, updated_at", (name, sponsor_id))
                row = cur.fetchone()
                if not row: raise HTTPException(status_code=404, detail="Sponsor not found.")
                return dict(row)
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="A sponsor with this name already exists.")
    finally: connection.close()

@router.patch("/{sponsor_id}/status")
def update_sponsor_status(sponsor_id: int, payload: SponsorStatus, current_user: dict = Depends(get_current_user)):
    connection = db()
    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("UPDATE public.sponsors SET is_active=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id, sponsor_name, is_active, created_at, updated_at", (payload.is_active, sponsor_id))
                row = cur.fetchone()
                if not row: raise HTTPException(status_code=404, detail="Sponsor not found.")
                return dict(row)
    finally: connection.close()
