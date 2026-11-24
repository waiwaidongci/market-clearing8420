from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.source import Dataset
from src.schemas.requests import SampleRequest
from src.services import sampling

router = APIRouter(prefix='/samples')


def _day(value):
    try:
        day = date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(422, '结算日须为 YYYY-MM-DD') from None
    if day != value:
        raise HTTPException(422, '结算日须为 YYYY-MM-DD')
    return day


def _require_dataset(db, dataset_id):
    if not db.get(Dataset, dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')


@router.get('/{day}')
def detail(day: str, db: Session = Depends(connection), actor: User = Depends(current)):
    return sampling.state(db, _day(day))


@router.post('/{day}/preview')
def preview(day: str, body: SampleRequest, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN, Role.ANALYST, Role.VIEWER))):
    _require_dataset(db, body.dataset_id)
    result = sampling.preview(db, _day(day), body.dataset_id, body)
    result["persisted"] = sampling.state(db, _day(day))
    return result


@router.post('/{day}/draw')
def draw(day: str, body: SampleRequest, db: Session = Depends(connection),
         actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    _require_dataset(db, body.dataset_id)
    result = sampling.draw(db, _day(day), body.dataset_id, body, actor.username)
    db.commit()
    return result
