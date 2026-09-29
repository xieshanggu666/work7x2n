"""配额台账并发原语：统一账户锁定、原子扣减、幂等流水与事务回滚。

所有涉及余额/流水变更的业务（交易划转、履约清缴、配额分配）都必须经此模块，
保证“账户锁定 → 余额校验 → 原子更新 → 写流水”在同一事务内完成：

- SQLite：由 engine 层统一发起 ``BEGIN IMMEDIATE``，事务一开始即持 RESERVED
  写锁，写事务全序串行，``FOR UPDATE`` 对 SQLite 无意义故不下发；
- PostgreSQL/MySQL：对账户行 ``SELECT ... FOR UPDATE``，多账户按 id 升序加锁防死锁；
- 扣减一律走条件更新 ``current_balance >= :amount``，以影响行数为准，
  即使锁机制失效也不会发生超额扣减；
- 幂等键（request_id）在账户内唯一，重复提交复用首笔流水；
- 任何异常由 :func:`ledger_transaction` 统一回滚，杜绝余额与流水不一致。
"""

from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.allowance import AllowanceAccount, AllowanceTransaction

INCREASE_TYPES = frozenset({"allocation", "buy", "transfer_in"})
DECREASE_TYPES = frozenset({"sell", "transfer_out", "offset", "clear"})
ALL_TX_TYPES = INCREASE_TYPES | DECREASE_TYPES


class InsufficientBalanceError(ValueError):
    """可用配额不足，拒绝扣减（调用方回滚后返回 400）。"""


@contextmanager
def ledger_transaction(db: Session):
    """台账写操作统一事务边界：正常提交，任何异常一律回滚后抛出。"""
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise


def lock_accounts(db: Session, accounts: list[AllowanceAccount]) -> list[AllowanceAccount]:
    """按账户 id 升序加锁，避免多账户操作交叉加锁导致死锁。

    SQLite 依赖 ``BEGIN IMMEDIATE`` 的库级写锁串行化，不下发行锁；
    其他数据库使用 ``SELECT ... FOR UPDATE``。
    """
    ordered = sorted(accounts, key=lambda a: a.id)
    ids = [a.id for a in ordered]
    if ids and db.get_bind().dialect.name != "sqlite":
        (
            db.query(AllowanceAccount)
            .filter(AllowanceAccount.id.in_(ids))
            .with_for_update()
            .all()
        )
    return ordered


def get_account(db: Session, company_id: int, year: int, *, for_update: bool = True):
    """取企业年度账户（不存在返回 None），可选加行锁。"""
    q = db.query(AllowanceAccount).filter(
        AllowanceAccount.company_id == company_id,
        AllowanceAccount.year == year,
    )
    if for_update and db.get_bind().dialect.name != "sqlite":
        q = q.with_for_update()
    return q.first()


def find_idempotent_tx(db: Session, account_id: int, request_id: str | None):
    """按幂等键查找首笔流水（重复提交时直接复用）。"""
    if not request_id:
        return None
    return (
        db.query(AllowanceTransaction)
        .filter(
            AllowanceTransaction.account_id == account_id,
            AllowanceTransaction.request_id == request_id,
        )
        .first()
    )


def apply_balance_change(
    db: Session,
    account: AllowanceAccount,
    tx_type: str,
    amount: float,
) -> Decimal:
    """条件原子更新余额，返回更新后的余额。

    扣减时把“余额充足”下推为 WHERE 条件，以数据库影响行数判定成功，
    杜绝读-判-写之间的 TOCTOU 超额扣减。
    """
    if tx_type not in ALL_TX_TYPES:
        raise ValueError(f"不支持的交易类型: {tx_type}")
    value = round(Decimal(str(amount)), 4)
    if value <= 0:
        raise ValueError("划转数量必须为正数")

    if tx_type in INCREASE_TYPES:
        new_balance = AllowanceAccount.current_balance + value
        cond = None
    else:
        new_balance = AllowanceAccount.current_balance - value
        cond = AllowanceAccount.current_balance >= value

    stmt = update(AllowanceAccount).where(AllowanceAccount.id == account.id)
    if cond is not None:
        stmt = stmt.where(cond)
    stmt = stmt.values(current_balance=new_balance)
    rowcount = db.execute(stmt).rowcount
    if rowcount != 1:
        raise InsufficientBalanceError("配额余额不足")

    db.refresh(account)
    return Decimal(str(account.current_balance))


def write_ledger(
    db: Session,
    account: AllowanceAccount,
    tx_type: str,
    amount: float,
    balance_after: Decimal,
    *,
    counterparty: str = "",
    price: float | None = None,
    tx_date: str = "",
    remark: str = "",
    request_id: str | None = None,
) -> AllowanceTransaction:
    """登记一条流水（调用方已完成余额原子更新，balance_after 与之严格一致）。"""
    tx = AllowanceTransaction(
        account_id=account.id,
        company_id=account.company_id,
        tx_type=tx_type,
        amount=round(Decimal(str(amount)), 4),
        counterparty=counterparty,
        price=round(Decimal(str(price)), 2) if price is not None else None,
        tx_date=tx_date,
        balance_after=balance_after,
        remark=remark,
        request_id=request_id or None,
    )
    db.add(tx)
    db.flush()  # 让 (account_id, request_id) 唯一冲突在事务内立即暴露
    return tx


def reconcile_account(db: Session, account: AllowanceAccount) -> Decimal:
    """对账并返回差额（0 表示账实一致）。

    期初余额 ``opening_balance`` 累计的是配额分配（每笔 allocation 已含在内），
    因此一致性恒等式为：

        当前余额 == 期初余额 + Σ 非分配流水带符号金额

    清缴/卖出为负向，买入/划入为正向。
    """
    flows = (
        db.query(AllowanceTransaction)
        .filter(AllowanceTransaction.account_id == account.id)
        .all()
    )
    signed = Decimal("0")
    for tx in flows:
        if tx.tx_type == "allocation":
            continue  # 已计入 opening_balance，避免重复
        signed += Decimal(str(tx.amount)) * (
            Decimal("1") if tx.tx_type in INCREASE_TYPES else Decimal("-1")
        )
    expected = Decimal(str(account.opening_balance)) + signed
    return round(Decimal(str(account.current_balance)) - expected, 4)
