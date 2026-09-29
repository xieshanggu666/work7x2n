from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class CompanyIn(BaseModel):
    code: str
    name: str
    industry: str = ""
    region: str = ""
    boundary_desc: str = ""


class ScopeIn(BaseModel):
    scope: str = Field(pattern="^[123]$")
    category: str = ""
    name: str = ""
    description: str = ""


class ActivityIn(BaseModel):
    scope_id: int
    year: int
    period: str = "monthly"
    activity_type: str
    unit: str = ""
    quantity: float
    data_source: str = ""


class FactorIn(BaseModel):
    factor_code: str
    name: str
    scope: str = Field(default="1", pattern="^[123]$")
    unit: str = "tCO2/单位"
    value: float
    source: str = ""
    valid_from: str = ""
    valid_to: str | None = None


class QuotaIn(BaseModel):
    company_id: int
    year: int
    baseline: float = 0
    allocation_amount: float
    adjustment: float = 0


class TransferIn(BaseModel):
    amount: float
    tx_type: str = "sell"
    counterparty: str = ""
    price: float | None = None
    tx_date: str = ""
    remark: str = ""
    # 客户端幂等键：双击/网络重试携带同一 request_id 时只成交一次
    request_id: str | None = Field(default=None, max_length=64)
