from fastapi import APIRouter, Depends

from src.config.database import db_session
from src.schemas.allocation import AllocationConfirmInput, AllocationPreviewInput
from src.services import allocations as service
from src.utils.security import current, writer

router = APIRouter()


@router.get('/samples/{ident}/allocations')
def listing(ident: int, db=Depends(db_session), user=Depends(current)):
    return service.listing(db, ident)


@router.post('/samples/{ident}/allocations/preview', status_code=201)
def preview(ident: int, data: AllocationPreviewInput, db=Depends(db_session), user=Depends(writer)):
    return service.preview(db, ident, data, user)


@router.get('/allocations/{batch_id}')
def detail(batch_id: int, db=Depends(db_session), user=Depends(current)):
    return service.detail(db, batch_id)


@router.post('/allocations/{batch_id}/confirm')
def confirm(batch_id: int, data: AllocationConfirmInput, db=Depends(db_session), user=Depends(writer)):
    return service.confirm(db, batch_id, data, user)
