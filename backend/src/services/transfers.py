from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import Transfer,Sample
from src.utils.serialization import record
from .common import save
def listing(db): return [record(x) for x in db.scalars(select(Transfer).order_by(Transfer.id))]
def create(db,data,user):
    # Lock the sample row: a confirmation in flight either sees this request
    # before it freezes the split, or this request sees the stopped sample.
    sample=db.scalar(select(Sample).where(Sample.id==data.sample_id).with_for_update())
    if not sample: raise HTTPException(404,'样本不存在')
    if sample.parent_id is not None: raise HTTPException(422,'子样本请通过所挂移交申请继续流转，不再单独接单')
    if not sample.accepting: raise HTTPException(422,'原样本已停止接单，请改用已分装的子样本')
    if data.quantity>sample.quantity: raise HTTPException(422,'移交量超过样本可用量')
    return save(db,Transfer(**data.model_dump(),status='pending'),user,'创建流转申请 '+data.code)
