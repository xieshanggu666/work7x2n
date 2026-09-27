"""配额交易台账：买入/卖出/划转，带余额校验。"""

from sqlalchemy.orm import Session

from app.models.allowance import AllowanceAccount, AllowanceTransaction

_INCREASE_TYPES = {"allocation", "buy", "transfer_in"}
_DECREASE_TYPES = {"sell", "transfer_out", "offset", "clear"}


def transfer(
    db: Session,
    account: AllowanceAccount,
    amount: float,
    tx_type: str,
    counterparty: str = "",
    price: float | None = None,
    tx_date: str = "",
    remark: str = "",
) -> AllowanceTransaction:
    """在配额账户上划转配额，校验可用余额充足后写流水。"""
    if amount <= 0:
        raise ValueError("划转数量必须为正数")
    if tx_type not in _INCREASE_TYPES | _DECREASE_TYPES:
        raise ValueError(f"不支持的交易类型: {tx_type}")

    balance = float(account.current_balance)
    if tx_type in _DECREASE_TYPES and balance < amount:
        raise ValueError("配额余额不足")

    balance_after = balance + amount if tx_type in _INCREASE_TYPES else balance - amount
    account.current_balance = round(balance_after, 4)

    tx = AllowanceTransaction(
        account_id=account.id,
        company_id=account.company_id,
        tx_type=tx_type,
        amount=round(amount, 4),
        counterparty=counterparty,
        price=round(price, 2) if price is not None else None,
        tx_date=tx_date,
        balance_after=round(balance_after, 4),
        remark=remark,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx
