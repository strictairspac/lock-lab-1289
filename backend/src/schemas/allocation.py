from pydantic import BaseModel, Field, field_validator

class PortionInput(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    quantity: int = Field(ge=1, le=100000)

    @field_validator('code')
    @classmethod
    def strip_code(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('分装编号不能为空')
        return value

class AllocationPreviewInput(BaseModel):
    portions: list[PortionInput] = Field(min_length=1, max_length=200)
    note: str = Field(default='', max_length=300)

class AllocationConfirmInput(BaseModel):
    request_key: str = Field(min_length=8, max_length=80)
