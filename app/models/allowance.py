from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String

from app.core.database import Base


class Quota(Base):
    """年度配额分配：免费配额与调整。"""

    __tablename__ = "quotas"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    baseline = Column(Numeric(18, 4), nullable=False, default=0)      # 历史基准排放
    allocation_amount = Column(Numeric(18, 4), nullable=False, default=0)  # 免费配额（tCO2）
    adjustment = Column(Numeric(18, 4), nullable=False, default=0)    # 调整量（可为负）
    total = Column(Numeric(18, 4), nullable=False, default=0)         # 最终配额
    status = Column(String(16), nullable=False, default="pending")    # pending/allocated/cleared
    allocated_at = Column(DateTime, nullable=True)


class AllowanceAccount(Base):
    """配额账户：企业年度配额持仓。"""

    __tablename__ = "allowance_accounts"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    opening_balance = Column(Numeric(18, 4), nullable=False, default=0)
    current_balance = Column(Numeric(18, 4), nullable=False, default=0)
    frozen_balance = Column(Numeric(18, 4), nullable=False, default=0)   # 履约冻结
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class AllowanceTransaction(Base):
    """配额划转与交易台账。"""

    __tablename__ = "allowance_transactions"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("allowance_accounts.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    tx_type = Column(String(16), nullable=False)   # allocation/buy/sell/transfer/offset/clear
    amount = Column(Numeric(18, 4), nullable=False, default=0)
    counterparty = Column(String(128), nullable=False, default="")
    price = Column(Numeric(18, 2), nullable=True)
    tx_date = Column(String(10), nullable=False, default="")
    balance_after = Column(Numeric(18, 4), nullable=False, default=0)
    remark = Column(String(256), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ComplianceRecord(Base):
    """年度履约记录：清缴配额抵扣实际排放。"""

    __tablename__ = "compliance_records"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    verified_emission = Column(Numeric(18, 4), nullable=False, default=0)  # 核查后排放量
    cleared_amount = Column(Numeric(18, 4), nullable=False, default=0)     # 已清缴配额
    deficit = Column(Numeric(18, 4), nullable=False, default=0)            # 缺口
    status = Column(String(16), nullable=False, default="pending")         # pending/compliant/deficit
    deadline = Column(String(10), nullable=False, default="")
    cleared_at = Column(DateTime, nullable=True)
