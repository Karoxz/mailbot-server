from pydantic import BaseModel
from typing import Optional, List
 
class TruckDef(BaseModel):
    vehicle: str
    driver_name: str
    dimensions: str
    max_payload_lbs: Optional[int] = None
    equipment: str = ''
    allowed_states: Optional[List[str]] = None
    zip_location: str
    pickup_date: str = ''
 
class ParseRequest(BaseModel):
    license_key: str
    machine_id: str
    email_body: str
    internal_date_ms: int
    allowed_vehicles: List[str]
    max_radius_miles: int
    trucks: List[TruckDef]
    bid_template: str
 
class ParseResponse(BaseModel):
    success: bool
    message: str
    formatted: Optional[str] = None
    vehicle_info: Optional[str] = None
    order_id: Optional[str] = None
    route_url: Optional[str] = None
    load_data: Optional[dict] = None
 
class ActivateRequest(BaseModel):
    license_key: str
    machine_id: str
    machine_name: str
 
class HeartbeatRequest(BaseModel):
    license_key: str
    machine_id: str
