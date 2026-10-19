"""Declarative base for all SQLAlchemy models. Imported by app/models/*."""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
