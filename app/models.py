from datetime import date, datetime, timezone
from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class Series(Base):
    __tablename__ = "series"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(String(100))
    dimension: Mapped[str] = mapped_column(String(300), default="Все данные")
    publication_id: Mapped[int] = mapped_column(Integer)
    dataset_id: Mapped[int] = mapped_column(Integer)
    measure_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    element_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    col_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_url: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(30), default="cbr")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    observations: Mapped[list["Observation"]] = relationship(back_populates="series")

class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    raw_period: Mapped[str] = mapped_column(String(100))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    series: Mapped[Series] = relationship(back_populates="observations")
    __table_args__ = (UniqueConstraint("series_id", "date", name="uq_series_period"),
                     Index("idx_obs_date", "date"))

class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    publication_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dataset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="cbr")
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

class SourceArtifact(Base):
    """Fingerprint of a successfully ingested source payload, not raw private data."""
    __tablename__ = "source_artifacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    retrieval: Mapped[str] = mapped_column(String(30), default="online")
    url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    bytes_count: Mapped[int] = mapped_column(Integer)
    checksum_method: Mapped[str] = mapped_column(String(40), default="sha256_raw_bytes")
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ObservationRevision(Base):
    """Append-only history of corrected historical statistics."""
    __tablename__ = "observation_revisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), index=True)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id"), nullable=True)
    date: Mapped[date] = mapped_column(Date)
    old_value: Mapped[float] = mapped_column(Float)
    new_value: Mapped[float] = mapped_column(Float)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
