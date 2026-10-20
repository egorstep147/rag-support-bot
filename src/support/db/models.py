"""Схема: источники → документы → куски (вектор + полнотекстовый индекс) и диалоги с сообщениями."""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Computed, DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBED_DIM = 768


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True), str: Text, dict: JSONB, list: JSONB}


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    kind: Mapped[str]  # files | faq | site | gdoc | notion | upload
    status: Mapped[str] = mapped_column(default="new")  # new | ok | error
    error: Mapped[str] = mapped_column(default="")
    synced_at: Mapped[datetime | None]
    documents: Mapped[list["Document"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("source_id", "uri"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    uri: Mapped[str]  # путь файла, адрес страницы, id документа — по нему узнаём документ при пересинхронизации
    title: Mapped[str]
    url: Mapped[str] = mapped_column(default="")  # ссылка для клиента в источниках ответа
    content_hash: Mapped[str]  # sha256 текста: не изменился — не пересчитываем эмбеддинги
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    source: Mapped[Source] = relationship(back_populates="documents", lazy="joined")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int]
    heading: Mapped[str] = mapped_column(default="")  # путь заголовков: «Тарифы › Турбо»
    text: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    # Полнотекстовый индекс по-русски считает сама база: заголовок весомее текста
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('russian', coalesce(heading, '')), 'A') || to_tsvector('russian', text)",
            persisted=True,
        ),
    )
    document: Mapped[Document] = relationship(back_populates="chunks", lazy="joined")


class Conversation(Base):
    """Диалог клиента. channel: tg (external_id — Telegram ID) или web (секрет сессии виджета)."""

    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("channel", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str]
    external_id: Mapped[str]
    name: Mapped[str] = mapped_column(default="")
    mode: Mapped[str] = mapped_column(default="ai")  # ai | operator — кто сейчас отвечает
    topic_id: Mapped[int | None] = mapped_column(BigInteger)  # тема в группе операторов
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conv", "conversation_id", "id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str]  # user | assistant | operator | system
    text: Mapped[str]
    sources: Mapped[list] = mapped_column(default=list)  # [{"title", "url"}] — на что сослался ответ
    answered: Mapped[bool | None]  # для ответов ИИ: нашёлся ли ответ в базе (False — «не знаю»)
    rating: Mapped[int | None]  # 👍 1 / 👎 -1
    tokens: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
