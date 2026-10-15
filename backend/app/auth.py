import os
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from .database import get_db
from .models import User

load_dotenv()
SECRET = os.getenv('SECRET_KEY', 'change-this-secret')
ALGO = 'HS256'
pwd = CryptContext(schemes=['bcrypt'], deprecated='auto')
bearer = HTTPBearer()

def hash_password(p): return pwd.hash(p)
def verify_password(p, h): return pwd.verify(p, h)
def create_token(user_id):
    return jwt.encode({'sub': str(user_id), 'exp': datetime.now(timezone.utc)+timedelta(hours=12)}, SECRET, algorithm=ALGO)

def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(creds.credentials, SECRET, algorithms=[ALGO])
        user = db.get(User, int(payload['sub']))
    except (JWTError, ValueError, KeyError):
        user = None
    if not user: raise HTTPException(401, 'Invalid or expired token')
    return user
