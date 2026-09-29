"""配额管理：年度配额分配与履约清缴。

并发安全统一由 :mod:`app.services.ledger` 提供：
- 分配/清缴均在单个写事务内完成“锁账户 → 校验 → 原子更新 → 写流水”；
- (企业, 年度) 唯一约束 + 保存点兜底并发插入，重复分配/清缴不重复入账；
- 异常一律回滚，余额与流水不可能出现半成功状态。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.allowance import (
    AllowanceAccount,
    ComplianceRecord,
    Quota,
)
from app.services.calculation_service import annual_total
from app.services.ledger import (
    apply_balance_change,
    get_account,
    ledger_transaction,
    lock_accounts,
    write_ledger,
)


def allocate_quota(
    db: Session,
    company_id: int,
    year: int,
    baseline: float,
    allocation_amount: float,
    adjustment: float = 0.0,
) -> Quota:
    """免费配额分配：写入配额、初始化账户、登记划入流水。

    同一企业同一年度的重复（含并发）分配直接返回已有配额，不重复入账。
    """
    with ledger_transaction(db):
        existing = (
            db.query(Quota)
            .filter(Quota.company_id == company_id, Quota.year == year)
            .first()
        )
        if existing:
            return existing

        total = round(Decimal(str(allocation_amount)) + Decimal(str(adjustment)), 4)
        quota = Quota(
            company_id=company_id,
            year=year,
            baseline=round(Decimal(str(baseline)), 4),
            allocation_amount=round(Decimal(str(allocation_amount)), 4),
            adjustment=round(Decimal(str(adjustment)), 4),
            total=total,
            status="allocated",
            allocated_at=datetime.utcnow(),
        )
        db.add(quota)
        try:
            # 保存点兜底：并发下唯一约束冲突时外层事务仍可复用已有行
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            existing = (
                db.query(Quota)
                .filter(Quota.company_id == company_id, Quota.year == year)
                .first()
            )
            if existing:
                return existing
            raise

        account = get_account(db, company_id, year)
        is_new = account is None
        if is_new:
            account = AllowanceAccount(
                company_id=company_id,
                year=year,
                opening_balance=total,
                current_balance=total,
                frozen_balance=0,
            )
            db.add(account)
            try:
                # 保存点兜底：并发下另一事务抢先创建账户时，复用其行并转为追加
                with db.begin_nested():
                    db.flush()
            except IntegrityError:
                is_new = False
                account = get_account(db, company_id, year)

        lock_accounts(db, [account])
        if is_new:
            # 新账户插入时余额已含本笔分配，无需再次条件更新
            balance_after = Decimal(str(account.current_balance))
        else:
            account.opening_balance = round(Decimal(str(account.opening_balance)) + total, 4)
            balance_after = apply_balance_change(db, account, "allocation", float(total))

        write_ledger(
            db,
            account,
            "allocation",
            float(total),
            balance_after,
            counterparty="主管部门",
            tx_date=datetime.utcnow().strftime("%Y-%m-%d"),
            remark=f"{year}年度免费配额分配",
        )
        db.refresh(quota)
        return quota


def _get_or_create_locked_record(
    db: Session, company_id: int, year: int, deadline: str
) -> ComplianceRecord:
    """取（并加锁）履约记录；不存在则在保存点内创建，兜住并发插入。"""
    q = db.query(ComplianceRecord).filter(
        ComplianceRecord.company_id == company_id,
        ComplianceRecord.year == year,
    )
    if db.get_bind().dialect.name != "sqlite":
        q = q.with_for_update()
    record = q.first()
    if record:
        return record

    record = ComplianceRecord(company_id=company_id, year=year, deadline=deadline)
    db.add(record)
    try:
        with db.begin_nested():
            db.flush()
        return record
    except IntegrityError:
        return (
            db.query(ComplianceRecord)
            .filter(
                ComplianceRecord.company_id == company_id,
                ComplianceRecord.year == year,
            )
            .with_for_update()
            .first()
        )


def clear_emission(db: Session, company_id: int, year: int, deadline: str) -> ComplianceRecord:
    """履约清缴：从配额账户划转与排放量等额的配额，缺口记为 deficit。

    清缴为一次性业务：已完成（compliant/deficit）的记录重复提交时直接返回
    原记录，余额与流水不发生任何变动，杜绝重复履约。无账户时不产生流水
    （历史实现会写入 account_id=0 的脏流水），全额记为缺口。
    """
    with ledger_transaction(db):
        account = get_account(db, company_id, year)
        record = _get_or_create_locked_record(db, company_id, year, deadline)

        if record.status in ("compliant", "deficit"):
            return record

        emission = round(Decimal(str(annual_total(db, company_id, year))), 4)
        balance = Decimal(str(account.current_balance)) if account else Decimal("0")
        cleared = min(balance, emission)
        deficit = round(emission - cleared, 4)

        if account and cleared > 0:
            lock_accounts(db, [account])
            balance_after = apply_balance_change(db, account, "clear", float(cleared))
            write_ledger(
                db,
                account,
                "clear",
                float(cleared),
                balance_after,
                counterparty="履约清缴",
                tx_date=deadline,
                remark=f"{year}年度履约清缴 {cleared} 吨配额",
            )

        record.verified_emission = emission
        record.cleared_amount = cleared
        record.deficit = deficit
        record.status = "compliant" if deficit <= 0 else "deficit"
        record.cleared_at = datetime.utcnow()
        db.flush()
        return record
