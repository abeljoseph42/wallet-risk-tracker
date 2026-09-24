"""Declarative base shared by all ORM models (models arrive in Phase 1)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
