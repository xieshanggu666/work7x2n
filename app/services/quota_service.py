"""配额管理：年度配额分配与履约清缴。"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.allowance import (
    AllowanceAccount,
    AllowanceTransaction,
    ComplianceRecord,
    Quota,
)
from app.services.calculation_service import annual_total


def allocate_quota(
    db: Session,
    company_id: int,
    year: int,
    baseline: float,
    allocation_amount: float,
    adjustment: float = 0.0,
) -> Quota:
    """免费配额分配：写入配额、初始化账户、登记划入流水。"""
    existing = db.query(Quota).filter(Quota.company_id == company_id, Quota.year == year).first()
    if existing:
        return existing

    total = round(allocation_amount + adjustment, 4)
    quota = Quota(
        company_id=company_id,
        year=year,
        baseline=round(baseline, 4),
        allocation_amount=round(allocation_amount, 4),
        adjustment=round(adjustment, 4),
        total=total,
        status="allocated",
        allocated_at=datetime.utcnow(),
    )
    db.add(quota)

    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    if account:
        account.current_balance = round(float(account.current_balance) + total, 4)
        account.opening_balance = round(float(account.opening_balance) + total, 4)
    else:
        account = AllowanceAccount(
            company_id=company_id,
            year=year,
            opening_balance=total,
            current_balance=total,
            frozen_balance=0,
        )
        db.add(account)
    db.flush()

    db.add(
        AllowanceTransaction(
            account_id=account.id,
            company_id=company_id,
            tx_type="allocation",
            amount=total,
            counterparty="主管部门",
            price=None,
            tx_date=datetime.utcnow().strftime("%Y-%m-%d"),
            balance_after=float(account.current_balance),
            remark=f"{year}年度免费配额分配",
        )
    )
    db.commit()
    db.refresh(quota)
    return quota


def clear_emission(db: Session, company_id: int, year: int, deadline: str) -> ComplianceRecord:
    """履约清缴：从配额账户划转与排放量等额的配额，缺口记为 deficit。"""
    emission = annual_total(db, company_id, year)
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    record = db.query(ComplianceRecord).filter(ComplianceRecord.company_id == company_id, ComplianceRecord.year == year).first()
    if not record:
        record = ComplianceRecord(company_id=company_id, year=year, deadline=deadline)
        db.add(record)
        db.flush()

    balance = float(account.current_balance) if account else 0.0
    cleared = round(min(balance, emission), 4)
    deficit = round(emission - cleared, 4)

    if account:
        account.current_balance = round(balance - cleared, 4)
    if cleared > 0:
        db.add(
            AllowanceTransaction(
                account_id=account.id if account else 0,
                company_id=company_id,
                tx_type="clear",
                amount=cleared,
                counterparty="履约清缴",
                price=None,
                tx_date=deadline,
                balance_after=float(account.current_balance) if account else 0.0,
                remark=f"{year}年度履约清缴 {cleared} 吨配额",
            )
        )

    record.verified_emission = emission
    record.cleared_amount = cleared
    record.deficit = deficit
    record.status = "compliant" if deficit <= 0 else "deficit"
    record.cleared_at = datetime.utcnow()
    db.commit()
    db.refresh(record)
    return record
