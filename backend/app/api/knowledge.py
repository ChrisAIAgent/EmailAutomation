"""Knowledge Base API for user-managed LangGraph context."""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..knowledge import retrieve_knowledge
from .deps import ensure_owner, get_db

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

MAX_FILE_BYTES = 2 * 1024 * 1024
ALLOWED_EXTENSIONS = {".txt": "text", ".md": "markdown", ".markdown": "markdown", ".csv": "csv"}
REPLY_STRATEGY_CATEGORY = "reply_strategy"


class KnowledgeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    category: str = Field(default="general", min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=500_000)
    publish: bool = False


class KnowledgeUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    content: str | None = Field(default=None, min_length=1, max_length=500_000)


class KnowledgeSearch(BaseModel):
    query: str = Field(min_length=1, max_length=5_000)
    limit: int = Field(default=4, ge=1, le=10)


def _hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def _out(document: models.KnowledgeDocument) -> dict:
    return {
        "id": document.id,
        "title": document.title,
        "category": document.category,
        "content": document.content,
        "source_type": document.source_type,
        "source_name": document.source_name,
        "status": document.status,
        "version": document.version,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
        "characters": len(document.content or ""),
    }


def _commit_document(db: Session, document: models.KnowledgeDocument, action: str) -> dict:
    try:
        db.add(document)
        db.flush()
        db.add(models.AuditLog(
            actor="user",
            action=action,
            entity="knowledge_document",
            entity_id=str(document.id),
            detail=f"title={document.title}; status={document.status}; version={document.version}",
        ))
        db.commit()
        db.refresh(document)
        return _out(document)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="duplicate_knowledge_content")


def _ensure_reply_strategy_publishable(
    db: Session, owner_id: int, *, exclude_document_id: int | None = None
) -> None:
    query = db.query(models.KnowledgeDocument).filter_by(
        owner_id=owner_id,
        category=REPLY_STRATEGY_CATEGORY,
        status="published",
    )
    if exclude_document_id is not None:
        query = query.filter(models.KnowledgeDocument.id != exclude_document_id)
    if query.first() is not None:
        raise HTTPException(status_code=409, detail="published_reply_strategy_already_exists")


@router.get("")
def list_documents(db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    # Legacy archived rows stay hidden until the operator explicitly deletes
    # them. New delete operations physically remove the document.
    q = db.query(models.KnowledgeDocument).filter_by(owner_id=owner_id).filter(
        models.KnowledgeDocument.status != "archived"
    )
    return [_out(row) for row in q.order_by(models.KnowledgeDocument.updated_at.desc()).all()]


@router.get("/{document_id}")
def get_document(document_id: int, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    document = db.query(models.KnowledgeDocument).filter_by(id=document_id, owner_id=owner_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="knowledge_document_not_found")
    return _out(document)


@router.post("")
def create_document(payload: KnowledgeCreate, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    content = payload.content.strip()
    title = payload.title.strip()
    category = payload.category.strip().lower()
    if not title or not category:
        raise HTTPException(status_code=422, detail="knowledge_title_and_category_required")
    if payload.publish and category == REPLY_STRATEGY_CATEGORY:
        _ensure_reply_strategy_publishable(db, owner_id)
    return _commit_document(db, models.KnowledgeDocument(
        owner_id=owner_id,
        title=title,
        category=category,
        content=content,
        source_type="pasted",
        content_hash=_hash(content),
        status="published" if payload.publish else "draft",
    ), "knowledge_created")


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    category: str = Form(default="general"),
    publish: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="supported_files: .txt, .md, .markdown, .csv")
    raw = await file.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="knowledge_file_too_large_max_2mb")
    try:
        content = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="knowledge_file_must_be_utf8")
    if not content:
        raise HTTPException(status_code=422, detail="knowledge_file_is_empty")
    owner_id = ensure_owner(db)
    document_title = (title or Path(file.filename or "Knowledge").stem).strip()[:300]
    document_category = (category or "general").strip().lower()[:80]
    if not document_title or not document_category:
        raise HTTPException(status_code=422, detail="knowledge_title_and_category_required")
    if publish and document_category == REPLY_STRATEGY_CATEGORY:
        _ensure_reply_strategy_publishable(db, owner_id)
    return _commit_document(db, models.KnowledgeDocument(
        owner_id=owner_id,
        title=document_title,
        category=document_category,
        content=content,
        source_type=ALLOWED_EXTENSIONS[suffix],
        source_name=(file.filename or "")[:300] or None,
        content_hash=_hash(content),
        status="published" if publish else "draft",
    ), "knowledge_uploaded")


@router.put("/{document_id}")
def update_document(document_id: int, payload: KnowledgeUpdate, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    document = db.query(models.KnowledgeDocument).filter_by(id=document_id, owner_id=owner_id).first()
    if not document or document.status == "archived":
        raise HTTPException(status_code=404, detail="knowledge_document_not_found")
    values = payload.model_dump(exclude_unset=True)
    if "title" in values:
        document.title = values["title"].strip()
    if "category" in values:
        document.category = values["category"].strip().lower()
    if "content" in values:
        document.content = values["content"].strip()
        document.content_hash = _hash(document.content)
    if document.status == "published" and document.category == REPLY_STRATEGY_CATEGORY:
        _ensure_reply_strategy_publishable(db, owner_id, exclude_document_id=document.id)
    document.version += 1
    return _commit_document(db, document, "knowledge_updated")


@router.post("/{document_id}/publish")
def publish_document(document_id: int, db: Session = Depends(get_db)):
    return _set_status(document_id, "published", db)


@router.post("/{document_id}/disable")
def disable_document(document_id: int, db: Session = Depends(get_db)):
    return _set_status(document_id, "disabled", db)


def _set_status(document_id: int, status: str, db: Session) -> dict:
    owner_id = ensure_owner(db)
    document = db.query(models.KnowledgeDocument).filter_by(id=document_id, owner_id=owner_id).first()
    if not document or document.status == "archived":
        raise HTTPException(status_code=404, detail="knowledge_document_not_found")
    if status == "published" and document.category == REPLY_STRATEGY_CATEGORY:
        _ensure_reply_strategy_publishable(db, owner_id, exclude_document_id=document.id)
    document.status = status
    return _commit_document(db, document, f"knowledge_{status}")


@router.delete("/{document_id}")
def delete_document(document_id: int, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    document = db.query(models.KnowledgeDocument).filter_by(id=document_id, owner_id=owner_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="knowledge_document_not_found")
    document_id_value = document.id
    db.add(models.AuditLog(
        actor="user",
        action="knowledge_deleted",
        entity="knowledge_document",
        entity_id=str(document_id_value),
        detail=f"title={document.title}; version={document.version}",
    ))
    db.delete(document)
    db.commit()
    return {"id": document_id_value, "deleted": True}


@router.post("/search")
def test_search(payload: KnowledgeSearch, db: Session = Depends(get_db)):
    owner_id = ensure_owner(db)
    return {
        "query": payload.query,
        "results": retrieve_knowledge(payload.query, db=db, owner_id=owner_id, limit=payload.limit),
    }
