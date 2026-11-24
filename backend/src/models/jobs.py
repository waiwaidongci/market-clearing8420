from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.constants.enums import Review, Status
from src.utils.time import now

class Schedule(Base):
    __tablename__ = 'schedules'

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    params: Mapped[dict] = mapped_column(JSON)
    recipient: Mapped[str] = mapped_column(String(100))
    next_at: Mapped[datetime] = mapped_column(index=True)
    repeat_daily: Mapped[bool]
    enabled: Mapped[bool] = mapped_column(default=True)
    author: Mapped[str] = mapped_column(String(64))

class Run(Base):
    __tablename__ = 'runs'
    __table_args__ = (UniqueConstraint('schedule_id', 'scheduled_at'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey('schedules.id'))
    scheduled_at: Mapped[datetime | None]
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default=Status.QUEUED, index=True)
    params: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
    export_file: Mapped[str | None] = mapped_column(String(100))
    review: Mapped[str] = mapped_column(String(20), default=Review.PENDING)
    review_note: Mapped[str | None] = mapped_column(String(500))
    decision: Mapped[str | None] = mapped_column(String(120))
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[datetime | None]

class Delivery(Base):
    __tablename__ = 'deliveries'

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'), unique=True)
    recipient: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=now)


class SampleDay(Base):
    """Per settlement-day sampling quota; the row lock serialises concurrent draws."""

    __tablename__ = 'sample_days'

    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    budget: Mapped[int] = mapped_column(default=0)
    zone_minimum: Mapped[int] = mapped_column(default=0)
    group_minimum: Mapped[int] = mapped_column(default=0)
    snapshot: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=now)


class SampleDraw(Base):
    """A voucher already drawn for a settlement day; one row per original redemption."""

    __tablename__ = 'sample_draws'
    __table_args__ = (UniqueConstraint('day', 'transaction_key'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    transaction_key: Mapped[str] = mapped_column(String(100))
    zone: Mapped[str | None] = mapped_column(String(80))
    merchant_group: Mapped[str | None] = mapped_column(String(80))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
