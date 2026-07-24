from pydantic import BaseModel, ConfigDict


class ItemCreate(BaseModel):
    name: str
    description: str = ""
    price: float = 0.0


class ItemOut(ItemCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
