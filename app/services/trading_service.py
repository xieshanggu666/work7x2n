"""配额交易台账：买入/卖出/划转。

并发安全由 :mod:`app.services.ledger` 统一保证：
账户锁定（SQLite BEGIN IMMEDIATE / 其他库 FOR UPDATE）→
余额条件原子扣减 → 幂等流水 → 统一提交/回滚。
"""

from sqlalchemy.orm import Session

from app.models.allowance import AllowanceAccount, AllowanceTransaction
from app.services.ledger import (
    ALL_TX_TYPES,
    apply_balance_change,
    find_idempotent_tx,
    ledger_transaction,
    lock_accounts,
    write_ledger,
)


def transfer(
    db: Session,
    account: AllowanceAccount,
    amount: float,
    tx_type: str,
    counterparty: str = "",
    price: float | None = None,
    tx_date: str = "",
    remark: str = "",
    request_id: str | None = None,
) -> AllowanceTransaction:
    """在配额账户上划转配额。

    - 重复提交：同一 ``request_id`` 直接返回首笔流水，余额不二次变动；
    - 余额不足：原子条件更新失败，抛 ``InsufficientBalanceError`` 并整体回滚；
    - 余额快照与流水同事务写入，保证账实一致。
    """
    if tx_type not in ALL_TX_TYPES:
        raise ValueError(f"不支持的交易类型: {tx_type}")

    # 幂等检查与账户锁定必须在同一写事务内
    with ledger_transaction(db):
        lock_accounts(db, [account])
        existing = find_idempotent_tx(db, account.id, request_id)
        if existing is not None:
            db.refresh(existing)
            return existing

        balance_after = apply_balance_change(db, account, tx_type, amount)
        tx = write_ledger(
            db,
            account,
            tx_type,
            amount,
            balance_after,
            counterparty=counterparty,
            price=price,
            tx_date=tx_date,
            remark=remark,
            request_id=request_id,
        )
        db.refresh(tx)
        return tx
