import secrets
from app.tasks import record_click
import json 
from fastapi import FastAPI, HTTPException,APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func as sql_func
from app.database import engine, Base, SessionLocal
from app import models
from fastapi.security import OAuth2PasswordBearer
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.redis_client import redis_client,rate_limit_script
from app.auth import hash_password, create_access_token, verify_access_token, verify_password

Base.metadata.create_all(bind=engine)

app = FastAPI()

api_router = APIRouter(prefix="/api/v1")

# session dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class LinkCreate(BaseModel):
    original_url: str

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

def encode_base62(num: int) -> str:
    if num == 0:
        return ALPHABET[0]
    
    result = []
    while num > 0:
        remainder = num % 62
        result.append(ALPHABET[remainder])
        num //= 62
    
    return "".join(reversed(result))
# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
oauth2_scheme = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    token = credentials.credentials
    payload = verify_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token"
        )
    user_id = payload.get("user_id")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

RATE_LIMIT = 10
RATE_WINDOW = 60

def check_rate_limit(current_user: models.User = Depends(get_current_user)):
    key = f"rate:user:{current_user.id}"
    count = rate_limit_script(
        keys=[key],
        args=[RATE_WINDOW]
    )
    if count > RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Max {RATE_LIMIT} requests per {RATE_WINDOW} seconds."
        )
    return current_user

@api_router.post("/links")
def create_link(link: LinkCreate, db: Session = Depends(get_db), current_user: models.User = Depends(check_rate_limit)):
    temp_code = secrets.token_urlsafe(4)
    new_link = models.Link(code=temp_code, original_url=link.original_url , user_id = current_user.id)
    db.add(new_link)
    db.commit()
    db.refresh(new_link)

    new_link.code = encode_base62(new_link.id)
    db.commit()
    db.refresh(new_link)
    return {"code": new_link.code, "original_url": new_link.original_url}

@api_router.get("/links")
def get_my_links(
    page: int = 1,
    page_size: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    offset = (page - 1) * page_size
    
    links = db.query(models.Link)\
        .filter(models.Link.user_id == current_user.id)\
        .order_by(models.Link.created_at.desc())\
        .offset(offset)\
        .limit(page_size)\
        .all()
    
    total = db.query(sql_func.count(models.Link.id))\
        .filter(models.Link.user_id == current_user.id)\
        .scalar()
    
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "links": [
            {"code": link.code, "original_url": link.original_url, "created_at": str(link.created_at)}
            for link in links
        ]
    }

@api_router.get("/{code}")
def redirect_link(code: str, request: Request, db: Session = Depends(get_db)):
    # check cache first
    cached = redis_client.get(f"link:{code}")
    if cached:
        data = json.loads(cached)#to parse string back from json
        record_click.delay(link_id=data["link_id"] , ip_address=request.client.host)
        return RedirectResponse(url=data["url"])
    
    # cache miss — query Postgres
    link = db.query(models.Link).filter(models.Link.code == code).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    
    # store in cache for next time — TTL of 24 hours
    redis_client.setex(f"link:{code}",
                    86400,
                    json.dumps({"url":link.original_url ,"link_id" : link.id}))
    
    record_click.delay(link_id=link.id, ip_address=request.client.host)
    return RedirectResponse(url=link.original_url)


@api_router.get("/links/{code}/stats")
def get_link_stats(code: str, db: Session = Depends(get_db)):
    cached = redis_client.get(f"stats:{code}")
    if cached:
        return json.loads(cached)
    # step 1 — find the link
    link = db.query(models.Link).filter(models.Link.code == code).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    
    # step 2 — total clicks
    total_clicks = db.query(sql_func.count(models.Click.id))\
        .filter(models.Click.link_id == link.id)\
        .scalar()
    
    # step 3 — clicks per day
    clicks_by_day = (
    db.query(
        sql_func.date(models.Click.clicked_at).label("click_date"),
        sql_func.count(models.Click.id).label("click_count")
    )
    .filter(models.Click.link_id == link.id)
    .group_by(sql_func.date(models.Click.clicked_at))
    .order_by(sql_func.date(models.Click.clicked_at))
    .all()
    )
    
    result = {
        "code": link.code,
        "original_url": link.original_url,
        "created_at": str(link.created_at),
        "total_clicks": total_clicks,
        "clicks_by_day": [
            {"date": str(row.click_date), "clicks": row.click_count}
            for row in clicks_by_day
        ]
    }
    # store in cache — 1 hour TTL
    redis_client.setex(f"stats:{code}", 3600, json.dumps(result))
    
    return result


class UserCreate(BaseModel):
    email: str
    password: str

@api_router.post("/auth/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    # check if email already exists
    existing_user = db.query(models.User).filter(
        models.User.email == user.email
    ).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    # hash the password and create the user
    hashed = hash_password(user.password)
    new_user = models.User(email=user.email, hashed_password=hashed)
    db.add(new_user)
    db.commit()

    # generate and return a token
    token = create_access_token({"user_id": new_user.id})
    return {"access_token": token, "token_type": "bearer"}


@api_router.post("/auth/login")
def login(user: UserCreate, db: Session = Depends(get_db)):
    # check if user exists
    db_user = db.query(models.User).filter(
        models.User.email == user.email
    ).first()
    
    if not db_user or not verify_password(user.password, db_user.hashed_password):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )
    
    token = create_access_token({"user_id": db_user.id})
    return {"access_token": token, "token_type": "bearer"}

app.include_router(api_router)