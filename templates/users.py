# users.py
from fastapi_users import FastAPIUsers
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from fastapi_users.manager import BaseUserManager, UUIDIDMixin
from fastapi_users.schemas import BaseUserCreate, BaseUserUpdate, BaseUserDB
from sqlalchemy import String, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from uuid import UUID, uuid4
from typing import Optional
from fastapi import Depends, Request
import datetime

DATABASE_URL = "sqlite+aiosqlite:///./users.db"

engine = create_async_engine(DATABASE_URL, echo=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)

from sqlalchemy.orm import DeclarativeBase
class Base(DeclarativeBase):
    pass

class User(SQLAlchemyBaseUserTableUUID, Base):
    username: Mapped[str] = mapped_column(String, nullable=False)

class UserRead(BaseUserDB):
    pass

class UserCreate(BaseUserCreate):
    username: str

class UserUpdate(BaseUserUpdate):
    username: Optional[str]

class UserManager(UUIDIDMixin, BaseUserManager[User, UUID]):
    reset_password_token_secret = "SECRET"
    verification_token_secret = "SECRET"

    async def on_after_register(self, user: User, request: Optional[Request] = None):
        print(f"User {user.id} registered")

async def get_user_db(session: AsyncSession = Depends(async_session_maker)):
    yield SQLAlchemyUserDatabase(session, User)

async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)

cookie_transport = CookieTransport(cookie_name="auth", cookie_max_age=86400)  # 24 hrs

def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret="SECRET", lifetime_seconds=86400)

auth_backend = AuthenticationBackend(
    name="jwt",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, UUID](
    get_user_manager,
    [auth_backend],
)

current_active_user = fastapi_users.current_user(active=True)
