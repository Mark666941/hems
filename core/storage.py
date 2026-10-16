"""資料層：使用者帳號與設定檔的儲存（SQLAlchemy，同一套程式碼支援 SQLite 與 PostgreSQL）。

- 桌面版：預設 SQLite，一個檔案（data/user/hems.db），不用安裝或設定任何東西；自動使用「本機使用者」免登入。
- 網頁版：設定環境變數 HEMS_DATABASE_URL（或 Streamlit secrets 的 database.url）為 PostgreSQL 網址，
  例如 Supabase：postgresql+pg8000://帳號:密碼@主機:5432/postgres；需要註冊、登入。

資料表：
- users：帳號、密碼雜湊（PBKDF2-SHA256 + 隨機鹽，不存明碼）、最後使用的設定檔
- profiles：每個使用者可有多組設定檔（例如「我家」「爸媽家」），內容是設定與介面選項的 JSON
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "user" / "hems.db"
LOCAL_USER = "本機使用者"
PBKDF2_ROUNDS = 200_000
EXPORT_VERSION = 1


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128), default="")     # 本機使用者沒有密碼
    salt: Mapped[str] = mapped_column(String(64), default="")
    last_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    settings_json: Mapped[str] = mapped_column(Text)
    ui_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@dataclass
class ProfileData:
    id: int
    name: str
    settings: dict
    ui: dict
    updated_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()


def resolve_url(url: str | None = None) -> str:
    """資料庫網址：參數 > 環境變數 HEMS_DATABASE_URL > 預設本機 SQLite 檔。"""
    url = url or os.environ.get("HEMS_DATABASE_URL")
    if url:
        return url
    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DB.as_posix()}"


class Store:
    """帳號與設定檔的存取。所有方法都在自己的交易中完成，失敗會回滾。"""

    def __init__(self, url: str | None = None):
        self.url = resolve_url(url)
        self.engine = create_engine(self.url, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)

    @property
    def is_local(self) -> bool:
        """本機 SQLite：桌面版，免登入。"""
        return self.engine.dialect.name == "sqlite"

    # ---------- 帳號 ----------

    def register(self, username: str, password: str) -> int:
        username = username.strip()
        if not 3 <= len(username) <= 32:
            raise ValueError("帳號長度需為 3–32 個字")
        if len(password) < 6:
            raise ValueError("密碼至少 6 個字")
        with Session(self.engine) as s, s.begin():
            if s.scalar(select(User).where(User.username == username)):
                raise ValueError("這個帳號已經有人使用")
            salt = secrets.token_hex(16)
            user = User(username=username, password_hash=hash_password(password, salt), salt=salt, created_at=_now())
            s.add(user)
            s.flush()
            return user.id

    def login(self, username: str, password: str) -> int | None:
        """帳號密碼正確回傳使用者 id，否則 None（不透露是帳號還是密碼錯）。"""
        with Session(self.engine) as s:
            user = s.scalar(select(User).where(User.username == username.strip()))
            if user is None or not user.password_hash:
                return None
            ok = hmac.compare_digest(user.password_hash, hash_password(password, user.salt))
            return user.id if ok else None

    def local_user(self) -> int:
        """桌面版的「本機使用者」（沒有密碼、不能從網頁登入）。"""
        with Session(self.engine) as s, s.begin():
            user = s.scalar(select(User).where(User.username == LOCAL_USER))
            if user is None:
                user = User(username=LOCAL_USER, created_at=_now())
                s.add(user)
                s.flush()
            return user.id

    def username(self, user_id: int) -> str:
        with Session(self.engine) as s:
            return s.get(User, user_id).username

    # ---------- 設定檔 ----------

    def list_profiles(self, user_id: int) -> list[tuple[int, str]]:
        with Session(self.engine) as s:
            rows = s.execute(select(Profile.id, Profile.name).where(Profile.user_id == user_id).order_by(Profile.id))
            return [(r.id, r.name) for r in rows]

    def create_profile(self, user_id: int, name: str, settings: dict, ui: dict) -> int:
        name = name.strip() or "未命名"
        with Session(self.engine) as s, s.begin():
            profile = Profile(user_id=user_id, name=name, settings_json=json.dumps(settings, ensure_ascii=False),
                              ui_json=json.dumps(ui, ensure_ascii=False, default=str), updated_at=_now())
            s.add(profile)
            s.flush()
            s.get(User, user_id).last_profile_id = profile.id
            return profile.id

    def load_profile(self, user_id: int, profile_id: int) -> ProfileData:
        with Session(self.engine) as s:
            p = s.get(Profile, profile_id)
            if p is None or p.user_id != user_id:
                raise KeyError("找不到這個設定檔")
            return ProfileData(p.id, p.name, json.loads(p.settings_json), json.loads(p.ui_json or "{}"), p.updated_at)

    def save_profile(self, user_id: int, profile_id: int, settings: dict, ui: dict) -> None:
        """自動存檔：覆寫設定與介面選項，並記為最後使用的設定檔。"""
        with Session(self.engine) as s, s.begin():
            p = s.get(Profile, profile_id)
            if p is None or p.user_id != user_id:
                raise KeyError("找不到這個設定檔")
            p.settings_json = json.dumps(settings, ensure_ascii=False)
            p.ui_json = json.dumps(ui, ensure_ascii=False, default=str)
            p.updated_at = _now()
            s.get(User, user_id).last_profile_id = profile_id

    def rename_profile(self, user_id: int, profile_id: int, name: str) -> None:
        with Session(self.engine) as s, s.begin():
            p = s.get(Profile, profile_id)
            if p is None or p.user_id != user_id:
                raise KeyError("找不到這個設定檔")
            p.name = name.strip() or p.name

    def delete_profile(self, user_id: int, profile_id: int) -> None:
        with Session(self.engine) as s, s.begin():
            if len(s.scalars(select(Profile.id).where(Profile.user_id == user_id)).all()) <= 1:
                raise ValueError("至少要保留一個設定檔")
            p = s.get(Profile, profile_id)
            if p is None or p.user_id != user_id:
                raise KeyError("找不到這個設定檔")
            s.delete(p)
            user = s.get(User, user_id)
            if user.last_profile_id == profile_id:
                user.last_profile_id = None

    def last_profile(self, user_id: int) -> int | None:
        """最後使用的設定檔；沒有記錄時回傳最早建立的一個；完全沒有設定檔時回傳 None。"""
        with Session(self.engine) as s:
            user = s.get(User, user_id)
            if user.last_profile_id and s.get(Profile, user.last_profile_id):
                return user.last_profile_id
            return s.scalar(select(Profile.id).where(Profile.user_id == user_id).order_by(Profile.id).limit(1))


# ---------- 匯出／匯入（JSON 檔，網頁版也能帶著走） ----------

def export_profile(profile: ProfileData) -> str:
    return json.dumps({"hems_profile_version": EXPORT_VERSION, "name": profile.name,
                       "exported_at": _now().isoformat(), "settings": profile.settings, "ui": profile.ui},
                      ensure_ascii=False, indent=2, default=str)


def parse_export(text: str) -> tuple[str, dict, dict]:
    """解析匯出的設定檔，回傳（名稱, settings, ui）。格式不對丟出 ValueError。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"不是有效的 JSON：{e}") from e
    if not isinstance(data, dict) or data.get("hems_profile_version") != EXPORT_VERSION:
        raise ValueError("這不是 HEMS 匯出的設定檔")
    settings = data.get("settings")
    if not isinstance(settings, dict) or not {"house", "schedule", "modes"} <= set(settings):
        raise ValueError("設定檔內容不完整")
    return str(data.get("name") or "匯入的設定"), settings, data.get("ui") or {}
