"""
Phase 4 unit tests — Authentication & Role-Based Access Control.

Covers:
- auth_service: hash_password, verify_password, create_access_token,
                decode_access_token, authenticate_user, create_user
- deps: get_current_user — valid token, expired/invalid token, inactive user
- API: POST /auth/login — success, wrong password, unknown user
- API: GET /auth/me — authenticated, unauthenticated
- Role guards: admin, export, preview role restrictions on protected endpoints
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import get_settings
from app.db.models import Base, Channel, User
from app.db.session import get_db
from app.models.schemas import ChannelConfig
from app.services.auth_service import (
    authenticate_user,
    create_access_token,
    create_user,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.api.v1 import auth as auth_router
from app.api.v1 import channels as channels_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def in_memory_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def db_session(in_memory_engine) -> Generator[Session, None, None]:
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)
    with SessionLocal() as session:
        yield session


def _make_channel(db: Session, channel_id: str = "rts1") -> Channel:
    cfg = ChannelConfig(
        id=channel_id,
        name="RTS1",
        display_name="RTS1 Test",
        paths={"record_dir": "/tmp/rec", "chunks_dir": "/tmp/chunks", "final_dir": "/tmp/final"},
    )
    ch = Channel(
        id=cfg.id,
        name=cfg.name,
        display_name=cfg.display_name,
        enabled=True,
        config_json=cfg.model_dump_json(),
    )
    db.add(ch)
    db.commit()
    db.refresh(ch)
    return ch


def _make_app(in_memory_engine) -> tuple[FastAPI, Session]:
    """Build a test FastAPI app with in-memory DB."""
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=in_memory_engine)
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1")
    app.include_router(channels_router.router, prefix="/api/v1")

    def override_db():
        with SessionLocal() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    return app, SessionLocal


# ---------------------------------------------------------------------------
# auth_service unit tests
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    def test_hash_and_verify(self):
        hashed = hash_password("secret123")
        assert hashed != "secret123"
        assert verify_password("secret123", hashed)

    def test_wrong_password(self):
        hashed = hash_password("correct")
        assert not verify_password("wrong", hashed)


class TestJWT:
    def test_create_and_decode(self):
        token = create_access_token("alice", "admin")
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == "alice"
        assert payload["role"] == "admin"

    def test_invalid_token(self):
        assert decode_access_token("not.a.token") is None

    def test_expired_token(self):
        settings = get_settings()
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        payload = {"sub": "alice", "role": "admin", "exp": past}
        token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
        assert decode_access_token(token) is None

    def test_wrong_secret(self):
        token = jwt.encode(
            {"sub": "alice", "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            "wrong-secret",
            algorithm="HS256",
        )
        assert decode_access_token(token) is None


class TestAuthenticateUser:
    def test_success(self, db_session: Session):
        create_user(db_session, "bob", "pass123", "export")
        user = authenticate_user(db_session, "bob", "pass123")
        assert user is not None
        assert user.username == "bob"
        assert user.role == "export"

    def test_wrong_password(self, db_session: Session):
        create_user(db_session, "bob", "pass123", "export")
        assert authenticate_user(db_session, "bob", "wrong") is None

    def test_unknown_user(self, db_session: Session):
        assert authenticate_user(db_session, "nobody", "anything") is None

    def test_inactive_user(self, db_session: Session):
        u = create_user(db_session, "carol", "pw", "preview")
        u.is_active = False
        db_session.commit()
        assert authenticate_user(db_session, "carol", "pw") is None


# ---------------------------------------------------------------------------
# API: POST /auth/login
# ---------------------------------------------------------------------------

class TestLoginEndpoint:
    def test_login_success(self, in_memory_engine):
        app, SessionLocal = _make_app(in_memory_engine)
        with SessionLocal() as db:
            create_user(db, "admin", "secret", "admin")
        client = TestClient(app)
        resp = client.post("/api/v1/auth/login", data={"username": "admin", "password": "secret"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert "access_token" in body
        assert body["username"] == "admin"
        assert body["role"] == "admin"

    def test_login_wrong_password(self, in_memory_engine):
        app, SessionLocal = _make_app(in_memory_engine)
        with SessionLocal() as db:
            create_user(db, "admin", "secret", "admin")
        client = TestClient(app)
        resp = client.post("/api/v1/auth/login", data={"username": "admin", "password": "WRONG"})
        assert resp.status_code == 401

    def test_login_unknown_user(self, in_memory_engine):
        app, SessionLocal = _make_app(in_memory_engine)
        client = TestClient(app)
        resp = client.post("/api/v1/auth/login", data={"username": "ghost", "password": "x"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# API: GET /auth/me
# ---------------------------------------------------------------------------

class TestMeEndpoint:
    def _token(self, username: str, role: str) -> str:
        return create_access_token(username, role)

    def test_me_authenticated(self, in_memory_engine):
        app, SessionLocal = _make_app(in_memory_engine)
        with SessionLocal() as db:
            create_user(db, "alice", "pw", "admin")
        client = TestClient(app)
        token = self._token("alice", "admin")
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == "alice"
        assert body["role"] == "admin"
        assert "password_hash" not in body

    def test_me_no_token(self, in_memory_engine):
        app, SessionLocal = _make_app(in_memory_engine)
        client = TestClient(app)
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_me_invalid_token(self, in_memory_engine):
        app, SessionLocal = _make_app(in_memory_engine)
        client = TestClient(app)
        resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer bad.token.here"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Role restrictions on channel endpoints
# ---------------------------------------------------------------------------

class TestChannelRoleGuards:
    """Verify that recording control actions require the admin role."""

    def _get_client_with_user(self, in_memory_engine, role: str) -> tuple[TestClient, str]:
        app, SessionLocal = _make_app(in_memory_engine)
        with SessionLocal() as db:
            create_user(db, "testuser", "pw", role)
            _make_channel(db)

        token = create_access_token("testuser", role)
        client = TestClient(app)
        return client, token

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    # --- Read endpoints: any authenticated role may call ---

    def test_list_channels_admin(self, in_memory_engine):
        client, token = self._get_client_with_user(in_memory_engine, "admin")
        resp = client.get("/api/v1/channels/", headers=self._auth(token))
        assert resp.status_code == 200

    def test_list_channels_export(self, in_memory_engine):
        client, token = self._get_client_with_user(in_memory_engine, "export")
        resp = client.get("/api/v1/channels/", headers=self._auth(token))
        assert resp.status_code == 200

    def test_list_channels_preview(self, in_memory_engine):
        client, token = self._get_client_with_user(in_memory_engine, "preview")
        resp = client.get("/api/v1/channels/", headers=self._auth(token))
        assert resp.status_code == 200

    def test_list_channels_unauthenticated(self, in_memory_engine):
        app, _ = _make_app(in_memory_engine)
        client = TestClient(app)
        resp = client.get("/api/v1/channels/")
        assert resp.status_code == 401

    # --- Admin-only: start ---

    def test_start_admin_allowed(self, in_memory_engine):
        """Admin may call start (will fail with 409/500 due to no real FFmpeg, not 403)."""
        client, token = self._get_client_with_user(in_memory_engine, "admin")
        with patch("app.services.process_manager.ProcessManager.start") as mock_start:
            from unittest.mock import MagicMock
            info = MagicMock()
            info.pid = 9999
            mock_start.return_value = info
            resp = client.post("/api/v1/channels/rts1/start", headers=self._auth(token))
        assert resp.status_code != 403

    def test_start_export_forbidden(self, in_memory_engine):
        client, token = self._get_client_with_user(in_memory_engine, "export")
        resp = client.post("/api/v1/channels/rts1/start", headers=self._auth(token))
        assert resp.status_code == 403

    def test_start_preview_forbidden(self, in_memory_engine):
        client, token = self._get_client_with_user(in_memory_engine, "preview")
        resp = client.post("/api/v1/channels/rts1/start", headers=self._auth(token))
        assert resp.status_code == 403

    # --- Admin-only: stop ---

    def test_stop_export_forbidden(self, in_memory_engine):
        client, token = self._get_client_with_user(in_memory_engine, "export")
        resp = client.post("/api/v1/channels/rts1/stop", headers=self._auth(token))
        assert resp.status_code == 403

    def test_stop_preview_forbidden(self, in_memory_engine):
        client, token = self._get_client_with_user(in_memory_engine, "preview")
        resp = client.post("/api/v1/channels/rts1/stop", headers=self._auth(token))
        assert resp.status_code == 403

    # --- Admin-only: restart ---

    def test_restart_export_forbidden(self, in_memory_engine):
        client, token = self._get_client_with_user(in_memory_engine, "export")
        resp = client.post("/api/v1/channels/rts1/restart", headers=self._auth(token))
        assert resp.status_code == 403

    # --- Admin-only: logs ---

    def test_logs_export_forbidden(self, in_memory_engine):
        client, token = self._get_client_with_user(in_memory_engine, "export")
        resp = client.get("/api/v1/channels/rts1/logs", headers=self._auth(token))
        assert resp.status_code == 403

    def test_logs_preview_forbidden(self, in_memory_engine):
        client, token = self._get_client_with_user(in_memory_engine, "preview")
        resp = client.get("/api/v1/channels/rts1/logs", headers=self._auth(token))
        assert resp.status_code == 403
