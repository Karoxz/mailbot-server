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
from typing import Optional

class RecordBidRequest(BaseModel):
    license_key:  str
    machine_id:   str
    order_id:     str
    thread_id:       Optional[str]   = None
    bid_method:      str = ""
    vehicle_type:    str = ""
    driver_name:     str = ""
    pickup_loc:      str = ""
    delivery_loc:    str = ""
    broker_name:     str = ""
    broker_email:    str = ""
    deadhead_miles:  Optional[float] = None
    loaded_miles:    Optional[float] = None
    total_miles:     Optional[float] = None
    verified_miles:  Optional[float] = None
    verified_source: Optional[str]   = None
    bid_amount:      Optional[float] = None
    
class ClassifyReplyRequest(BaseModel):
    license_key:  str
    machine_id:   str
    thread_id:    str
    subject:      str = ""
    message_body: str = ""


# ── NEW: update the bid_amount on an already-recorded bid row ──────────
# Used by both the driver-bot rate capture and the dispatcher-side
# rate-confirmation prompt, since bid_amount is usually unknown at the
# moment record_bid() first runs.
class UpdateBidAmountRequest(BaseModel):
    license_key: str
    machine_id:  str
    bid_id:      int
    bid_amount:  float
    
class ThreadLearningToggleRequest(BaseModel):
    license_key: str
    machine_id:  str
    
class ThreadMessageIn(BaseModel):
    message_id: str
    date_ms:    int
    is_from_me: bool
    subject:    str = ""
    body:       str = ""


class BackfillThreadRequest(BaseModel):
    license_key: str
    machine_id:  str
    thread_id:   str
    order_id:    Optional[str] = None       # if the client already knows it
    messages:    List[ThreadMessageIn]