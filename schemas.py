from pydantic import BaseModel, Field, ConfigDict

class ItemCreate(BaseModel):
    emp_code: str = Field(..., alias="EMP_CODE")
    punch_datetime: str = Field(..., alias="PUNCH_DATETIME")
    area_name: str = Field(..., alias="AREA_NAME")
    terminal_sn: str = Field(..., alias="TERMINAL_SN")

    class Config:
        allow_population_by_field_name = True

class ItemResponse(ItemCreate):
    id: int = Field(..., alias="ID")

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True
    )

    #class Config:
    #    orm_mode = True
    #    allow_population_by_field_name = True
