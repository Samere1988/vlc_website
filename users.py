import os
from typing import Optional
from uuid import UUID

from fastapi import Depends, Request
from fastapi_users import FastAPIUsers, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from fastapi_users.manager import BaseUserManager, UUIDIDMixin
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType

# ─── EMAIL CONFIGURATION (EDIT THIS) ─────────────────────────────
MAIL_USERNAME = "peakdecline@gmail.com"  # <--- PUT YOUR GMAIL HERE
MAIL_PASSWORD = "etjc bmzj uvqv uzpm "  # <--- PUT YOUR 16-CHAR APP PASSWORD HERE
MAIL_FROM = "peakdecline@gmail.com"
MAIL_PORT = 587
MAIL_SERVER = "smtp.gmail.com"
MAIL_FROM_NAME = "Peak Decline Admin"

# Create the mail config object
mail_conf = ConnectionConfig(
    MAIL_USERNAME=MAIL_USERNAME,
    MAIL_PASSWORD=MAIL_PASSWORD,
    MAIL_FROM=MAIL_FROM,
    MAIL_PORT=MAIL_PORT,
    MAIL_SERVER=MAIL_SERVER,
    MAIL_FROM_NAME=MAIL_FROM_NAME,
    MAIL_STARTTLS=True,
    MAIL_SSL_TLS=False,
    USE_CREDENTIALS=True,
    VALIDATE_CERTS=True
)

# ─── DATABASE SETUP ──────────────────────────────────────────────
DATABASE_URL = "sqlite+aiosqlite:///./users.db"
engine = create_async_engine(DATABASE_URL, echo=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def get_async_session():
    async with async_session_maker() as session:
        yield session


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    username: Mapped[str] = mapped_column(String, nullable=False)


# ─── SCHEMAS ─────────────────────────────────────────────────────
class UserRead(schemas.BaseUser[UUID]):
    username: str


class UserCreate(schemas.BaseUserCreate):
    username: str


class UserUpdate(schemas.BaseUserUpdate):
    username: Optional[str] = None


# ─── USER MANAGER (LOGIC) ────────────────────────────────────────
class UserManager(UUIDIDMixin, BaseUserManager[User, UUID]):
    reset_password_token_secret = "SECRET"
    verification_token_secret = "SECRET"

    async def on_after_register(self, user: User, request: Optional[Request] = None):
        print(f"User {user.id} registered")
        # Optional: Send Welcome Email here if you want

    async def on_after_forgot_password(self, user: User, token: str, request: Optional[Request] = None):
        # 1. Detect the current domain (localhost or your external IP)
        # If request is available, use it to get the base URL, otherwise default to localhost
        base_url = str(request.base_url) if request else "http://localhost:8000/"

        # Ensure base_url doesn't double slash (http://site.com//reset)
        reset_link = f"{base_url}reset-password/{token}"

        print(f"📧 Sending Reset Email to {user.email}")

        # 2. Create the Email content
        html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px;">
                <div style="max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1);">
                    <h2 style="color: #333;">Password Reset Request</h2>
                    <p>Hello {user.username},</p>
                    <p>We received a request to reset your password for Peak Decline.</p>
                    <p>Click the button below to reset it:</p>
                    <a href="{reset_link}" style="display: inline-block; background-color: #ff4444; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; margin-top: 10px;">Reset Password</a>
                    <p style="margin-top: 20px; font-size: 12px; color: #888;">If you didn't ask for this, you can ignore this email. The link expires in 24 hours.</p>
                </div>
            </body>
        </html>
        """

        message = MessageSchema(
            subject="Reset Your Peak Decline Password",
            recipients=[user.email],
            body=html,
            subtype=MessageType.html
        )

        # 3. Send the Email
        fm = FastMail(mail_conf)
        await fm.send_message(message)


async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    yield SQLAlchemyUserDatabase(session, User)


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


# ─── AUTH CONFIG ─────────────────────────────────────────────────
cookie_transport = CookieTransport(
    cookie_name="auth",
    cookie_max_age=86400,
    cookie_secure=False  # Set to True if you eventually use HTTPS (SSL)
)


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