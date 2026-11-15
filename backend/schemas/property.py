from typing import Optional
from pydantic import BaseModel, Field

class Property(BaseModel):
    wholesaler_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    property_type: Optional[str] = None
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    square_feet: Optional[int] = None
    asking_price: Optional[float] = Field(default=None, ge=0)
    arv: Optional[float] = Field(default=None, ge=0)
    repairs: Optional[float] = Field(default=None, ge=0)
    closing_costs: Optional[float] = Field(default=None, ge=0)
    assignment_fee: Optional[float] = Field(default=None, ge=0)
    selling_costs: Optional[float] = Field(default=None, ge=0)
    occupancy: Optional[str] = None
    condition: Optional[str] = None
    closing_timeline: Optional[str] = None
    additional_terms: Optional[str] = None
    contradictions: list[str] = []


class Properties(BaseModel):
    properties: list[Property]


