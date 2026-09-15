"""Skill library REST endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from webapp.config import load_settings
from webapp.server.schemas import CreateSkillRequest, UpdateSkillRequest
from webapp.skills.library import SkillLibrary
from webapp.store import db

router = APIRouter(prefix="/api/skills", tags=["skills"])
_library: SkillLibrary | None = None


def get_library() -> SkillLibrary:
    global _library
    if _library is None:
        _library = SkillLibrary(load_settings())
    return _library


@router.get("")
def list_skills(category: str | None = Query(None), enabled: bool | None = Query(None)):
    return {"skills": get_library().list_skills(category=category, enabled=enabled)}


@router.post("", status_code=201)
def create_skill(req: CreateSkillRequest):
    settings = load_settings()
    if req.category not in settings.skill_categories:
        raise HTTPException(400, f"category 必须是 {', '.join(settings.skill_categories)} 之一")
    try:
        skill = get_library().create(req.category, req.statement, None, None, None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not skill:
        raise HTTPException(409, "完全相同的经验已存在")
    return skill


@router.patch("/{skill_id}")
def update_skill(skill_id: str, req: UpdateSkillRequest):
    fields = req.model_dump(exclude_none=True)
    if fields.get("category") not in (None, *load_settings().skill_categories):
        raise HTTPException(400, "无效的 skill category")
    try:
        updated = get_library().update(skill_id, fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not updated:
        raise HTTPException(404, "skill 不存在")
    return updated


@router.delete("/{skill_id}", status_code=204)
def delete_skill(skill_id: str):
    get_library().delete(skill_id)
