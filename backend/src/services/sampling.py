"""Persist settlement-day sampling quotas and draw vouchers under a day lock."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.domain.analysis import validate_rows
from src.domain.sampling import plan
from src.models.jobs import SampleDay, SampleDraw
from src.models.source import Dataset
from src.services.audit import record
from src.utils.time import iso, now


def _clamp(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name}须为不小于 0 的整数")
    if value > 500:
        raise ValueError(f"{name}不能超过 500")
    return value


def _settings(body):
    return (_clamp(body.budget, "当日抽查预算"), _clamp(body.zone_minimum, "校区最低抽查数"),
            _clamp(body.group_minimum, "档口组最低抽查数"))


def _lock_day(db, day):
    """Fetch the day row under a row lock, creating it first when absent."""
    for _ in range(3):
        row = db.scalar(select(SampleDay).where(SampleDay.day == day).with_for_update())
        if row is not None:
            return row
        try:
            with db.begin_nested():
                db.add(SampleDay(day=day))
                db.flush()
        except IntegrityError:
            # Another transaction created the day row first; retry the locked read.
            pass
    row = db.scalar(select(SampleDay).where(SampleDay.day == day).with_for_update())
    if row is None:
        raise RuntimeError('无法锁定当日抽查名额，请重试')
    return row


def _draws(db, day):
    return [item.transaction_key for item in
            db.scalars(select(SampleDraw).where(SampleDraw.day == day).order_by(SampleDraw.id))]


def _dataset_rows(db, dataset_id):
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise LookupError("找不到指定的数据批次")
    return validate_rows(dataset.rows)


def state(db, day):
    item = db.get(SampleDay, day)
    draws = [{"transaction_key": draw.transaction_key, "zone": draw.zone,
              "merchant_group": draw.merchant_group, "author": draw.author}
             for draw in db.scalars(select(SampleDraw).where(SampleDraw.day == day).order_by(SampleDraw.id))]
    return {
        "day": day,
        "budget": item.budget if item else 0,
        "zone_minimum": item.zone_minimum if item else 0,
        "group_minimum": item.group_minimum if item else 0,
        "draws": draws,
        "result": item.snapshot if item else None,
        "updated_at": iso(item.updated_at) if item else None,
    }


def preview(db, day, dataset_id, body):
    budget, zone_minimum, group_minimum = _settings(body)
    rows = _dataset_rows(db, dataset_id)
    return plan(rows, day, budget, zone_minimum, group_minimum, _draws(db, day))


def draw(db, day, dataset_id, body, actor):
    """Draw the minimum vouchers needed, holding the day row lock until commit."""
    budget, zone_minimum, group_minimum = _settings(body)
    rows = _dataset_rows(db, dataset_id)
    item = _lock_day(db, day)
    item.budget, item.zone_minimum, item.group_minimum = budget, zone_minimum, group_minimum
    result = plan(rows, day, budget, zone_minimum, group_minimum, _draws(db, day))
    if result["drawn"]:
        drawn = {voucher["transaction_key"]: voucher for voucher in result["vouchers"]}
        for key in result["drawn"]:
            voucher = drawn[key]
            db.add(SampleDraw(day=day, transaction_key=key, zone=voucher["zone"],
                              merchant_group=voucher["merchant_group"], author=actor))
        db.flush()
        record(db, actor, "sample.drawn", day, keys=result["drawn"], budget=budget)
    elif result["status"] == "blocked":
        record(db, actor, "sample.withheld", day, message=result["message"],
               shortfall=result["shortfall"], budget=budget)
    else:
        record(db, actor, "sample.covered", day, message=result["message"], budget=budget)
    result["occupied"] = len(_draws(db, day))
    result["remaining"] = max(0, budget - result["occupied"])
    item.snapshot = result
    item.updated_at = now()
    db.flush()
    return result
