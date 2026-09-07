"""MySQL repository for user accounts."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from config import settings
from memory.models import Base, UserAccount
from memory.time_utils import utc_now


class AuthRepository:
    def __init__(self) -> None:
        self._engine = None
        self._SessionLocal = None

    def _ensure_ready(self) -> None:
        if self._SessionLocal is not None:
            return
        self._engine = create_engine(
            settings.mysql_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        self._SessionLocal = sessionmaker(bind=self._engine, expire_on_commit=False)
        Base.metadata.create_all(self._engine, tables=[UserAccount.__table__])

    @staticmethod
    def _as_dict(account: UserAccount) -> Dict[str, Any]:
        return {
            "user_id": account.user_id,
            "username": account.username,
            "is_active": bool(account.is_active),
        }

    def create_user(self, username: str, password_hash: str) -> Dict[str, Any]:
        self._ensure_ready()
        account = UserAccount(
            user_id=str(uuid.uuid4()),
            username=username,
            password_hash=password_hash,
        )
        try:
            with self._SessionLocal() as session:
                session.add(account)
                session.commit()
                session.refresh(account)
                return self._as_dict(account)
        except IntegrityError as exc:
            raise ValueError("username already exists") from exc

    def get_by_username(self, username: str) -> Optional[UserAccount]:
        self._ensure_ready()
        with self._SessionLocal() as session:
            account = session.execute(
                select(UserAccount).where(UserAccount.username == username)
            ).scalar_one_or_none()
            if account is None:
                return None
            session.expunge(account)
            return account

    def mark_login(self, user_id: str) -> None:
        self._ensure_ready()
        with self._SessionLocal() as session:
            account = session.execute(
                select(UserAccount).where(UserAccount.user_id == user_id)
            ).scalar_one_or_none()
            if account is not None:
                account.last_login_at = utc_now()
                session.commit()


auth_repository = AuthRepository()
