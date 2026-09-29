"""并发清缴/交易一致性回归测试。

覆盖三类风险：
1. 超额扣减：并发卖出同一账户，成交总额不得超过余额（原子条件更新 + 写锁）；
2. 重复履约/重复提交：重复清缴、重复 request_id、并发分配都只生效一次；
3. 失败回滚：业务失败时余额与流水保持一致，无 account_id=0 之类脏数据。
"""

import tempfile
import threading
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, create_app_engine
from app.models import (
    ActivityData,
    AllowanceAccount,
    AllowanceTransaction,
    Company,
    ComplianceRecord,
    Quota,
)
from app.services.calculation_service import recalc_company_year
from app.services.ledger import InsufficientBalanceError, reconcile_account
from app.services.quota_service import allocate_quota, clear_emission
from app.services.trading_service import transfer


def _add_activity(db, seed, scope_id, qty, year=2025):
    db.add(
        ActivityData(
            company_id=seed["company"].id,
            scope_id=scope_id,
            year=year,
            period="monthly",
            activity_type="外购电力",
            unit="MWh",
            quantity=qty,
            data_source="并发测试台账",
            verified=1,
        )
    )
    db.commit()


# ---------- 1. 原子扣减：余额校验下推到数据库，杜绝 TOCTOU ----------

class TestAtomicDeduction:
    def test_concurrent_sell_only_one_succeeds(self, tmp_path):
        """5 线程各卖出 600（余额 1000），必须恰好 1 笔成交、余额 400。"""
        engine = create_app_engine(f"sqlite:///{tmp_path}/sell.db")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        s = Session()
        company = Company(code="CC1", name="并发企业", industry="电力", region="测试")
        s.add(company)
        s.flush()
        acc = AllowanceAccount(company_id=company.id, year=2025,
                               opening_balance=1000, current_balance=1000)
        s.add(acc)
        s.commit()
        acc_id = acc.id
        s.close()

        outcomes = []

        def worker(i, req_id):
            sess = Session()
            try:
                a = sess.get(AllowanceAccount, acc_id)
                tx = transfer(sess, a, 600, "sell", counterparty=f"w{i}",
                              tx_date="2025-06-01", request_id=req_id)
                outcomes.append(("ok", tx.id))
            except InsufficientBalanceError:
                outcomes.append(("short", i))
            except Exception as e:  # noqa: BLE001
                outcomes.append(("error", str(e)[:100]))
            finally:
                sess.close()

        threads = [
            threading.Thread(target=worker, args=(i, f"req-{i}")) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        s = Session()
        final_balance = float(s.get(AllowanceAccount, acc_id).current_balance)
        tx_count = s.query(AllowanceTransaction).count()
        s.close()

        oks = [o for o in outcomes if o[0] == "ok"]
        assert len(oks) == 1, outcomes
        assert tx_count == 1
        assert final_balance == pytest.approx(400.0)
        assert all(o[0] in ("ok", "short") for o in outcomes)

    def test_insufficient_balance_full_rollback(self, db, seed):
        """卖出超额被拒：不写流水、余额不变、会话仍可继续使用。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        account = db.query(AllowanceAccount).first()

        with pytest.raises(InsufficientBalanceError):
            transfer(db, account, 900, "sell", tx_date="2025-06-01")

        db.expire_all()
        account = db.query(AllowanceAccount).first()
        assert float(account.current_balance) == pytest.approx(800)
        # 仅一条分配流水，失败的卖出不留任何痕迹
        assert db.query(AllowanceTransaction).count() == 1

        # 回滚后会话干净，后续正常交易不受影响
        tx = transfer(db, account, 100, "sell", tx_date="2025-06-02")
        assert float(tx.balance_after) == pytest.approx(700)


# ---------- 2. 幂等：重复提交只生效一次 ----------

class TestIdempotency:
    def test_same_request_id_reuses_first_tx(self, db, seed):
        """同一 request_id 重复提交：返回同一笔流水，余额只变动一次。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        account = db.query(AllowanceAccount).first()

        tx1 = transfer(db, account, 300, "sell", counterparty="甲",
                       tx_date="2025-06-01", request_id="uuid-abc")
        tx2 = transfer(db, account, 300, "sell", counterparty="甲",
                       tx_date="2025-06-01", request_id="uuid-abc")

        assert tx1.id == tx2.id
        assert db.query(AllowanceTransaction).count() == 2  # 分配 + 1 笔卖出
        db.expire_all()
        assert float(db.query(AllowanceAccount).first().current_balance) == pytest.approx(500)

    def test_same_request_id_different_accounts_isolated(self, db, seed):
        """幂等键按账户隔离：不同账户使用相同 request_id 互不影响。"""
        from app.models import EmissionScope

        other = Company(code="T-002", name="其他企业", industry="水泥", region="测试区")
        db.add(other)
        db.flush()
        db.add(EmissionScope(company_id=other.id, scope="2", category="电", name="用电"))
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        allocate_quota(db, other.id, 2025, 1000, 500, 0)

        a1 = db.query(AllowanceAccount).filter(AllowanceAccount.company_id == seed["company"].id).first()
        a2 = db.query(AllowanceAccount).filter(AllowanceAccount.company_id == other.id).first()
        t1 = transfer(db, a1, 100, "sell", request_id="shared-key", tx_date="2025-06-01")
        t2 = transfer(db, a2, 100, "sell", request_id="shared-key", tx_date="2025-06-01")
        assert t1.id != t2.id

    def test_duplicate_clear_does_not_deduct_twice(self, db, seed):
        """核心回归：清缴完成后再次清缴，不得二次扣减/重复履约。"""
        _add_activity(db, seed, seed["scope2"].id, 1000)
        recalc_company_year(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, 1000, 1000, 0)

        r1 = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        cleared_first = float(r1.cleared_amount)
        account = db.query(AllowanceAccount).first()
        balance_after_first = float(account.current_balance)
        clear_txs_after_first = (
            db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "clear").count()
        )

        # 重复清缴（双击/重试）
        r2 = clear_emission(db, seed["company"].id, 2025, "2025-12-31")

        assert r2.id == r1.id
        assert float(r2.cleared_amount) == pytest.approx(cleared_first)
        db.expire_all()
        account = db.query(AllowanceAccount).first()
        assert float(account.current_balance) == pytest.approx(balance_after_first)
        assert (
            db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "clear").count()
            == clear_txs_after_first == 1
        )
        assert db.query(ComplianceRecord).count() == 1

    def test_concurrent_clear_deducts_once(self, tmp_path):
        """并发清缴同一企业同一年度：只扣一次、只一条清缴流水。"""
        engine = create_app_engine(f"sqlite:///{tmp_path}/clear.db")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        s = Session()
        company = Company(code="CC2", name="清缴并发企业", industry="电力", region="测试")
        s.add(company)
        s.flush()
        s.add(ActivityData(
            company_id=company.id, scope_id=None, year=2025, period="monthly",
            activity_type="外购电力", unit="MWh", quantity=1000,
            data_source="台账", verified=1,
        ))
        s.add(AllowanceAccount(company_id=company.id, year=2025,
                               opening_balance=1000, current_balance=1000))
        s.commit()
        cid = company.id
        s.close()

        # 核算结果预先算好（annual_total 只读 EmissionResult）
        from app.models import EmissionResult
        s = Session()
        s.add(EmissionResult(company_id=cid, scope_id=None, year=2025, activity_id=1,
                             factor_id=None, method_code="", activity_quantity=1000,
                             factor_value=1, emission_amount=1000))
        s.commit()
        s.close()

        errors = []

        def worker():
            sess = Session()
            try:
                clear_emission(sess, cid, 2025, "2025-12-31")
            except Exception as e:  # noqa: BLE001
                errors.append(str(e)[:120])
            finally:
                sess.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        s = Session()
        assert not errors, errors
        account = s.query(AllowanceAccount).filter(AllowanceAccount.company_id == cid).first()
        assert float(account.current_balance) == pytest.approx(0.0)
        assert s.query(AllowanceTransaction).filter(
            AllowanceTransaction.tx_type == "clear").count() == 1
        assert s.query(ComplianceRecord).filter(
            ComplianceRecord.company_id == cid).count() == 1
        s.close()

    def test_concurrent_allocation_single_account(self, tmp_path):
        """并发分配同一企业同一年度：只有一份配额、一个账户、一条分配流水。"""
        engine = create_app_engine(f"sqlite:///{tmp_path}/alloc.db")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        s = Session()
        company = Company(code="CC3", name="分配并发企业", industry="电力", region="测试")
        s.add(company)
        s.commit()
        cid = company.id
        s.close()

        errors = []

        def worker():
            sess = Session()
            try:
                allocate_quota(sess, cid, 2025, 1000, 750, 0)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {str(e)[:100]}")
            finally:
                sess.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        s = Session()
        assert not errors, errors
        assert s.query(Quota).filter(Quota.company_id == cid).count() == 1
        accounts = s.query(AllowanceAccount).filter(AllowanceAccount.company_id == cid).all()
        assert len(accounts) == 1
        assert float(accounts[0].current_balance) == pytest.approx(750.0)
        assert float(accounts[0].opening_balance) == pytest.approx(750.0)
        assert s.query(AllowanceTransaction).filter(
            AllowanceTransaction.company_id == cid,
            AllowanceTransaction.tx_type == "allocation").count() == 1
        s.close()


# ---------- 3. 账实一致 / 失败回滚 / 边界 ----------

class TestConsistency:
    def test_no_dirty_tx_when_account_missing(self, db, seed):
        """无配额账户清缴：全额缺口，不得写 account_id=0 的脏流水。"""
        _add_activity(db, seed, seed["scope2"].id, 500)
        recalc_company_year(db, seed["company"].id, 2025)

        record = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert record.status == "deficit"
        assert float(record.deficit) == pytest.approx(
            float(record.verified_emission)
        )
        assert db.query(AllowanceTransaction).count() == 0

    def test_balance_matches_ledger_after_mixed_ops(self, db, seed):
        """混合操作后余额 == 流水重放结果（账实一致）。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        account = db.query(AllowanceAccount).first()
        transfer(db, account, 300, "sell", tx_date="2025-06-01", request_id="r1")
        transfer(db, account, 100, "buy", tx_date="2025-06-02", request_id="r2")
        transfer(db, account, 200, "sell", tx_date="2025-06-03", request_id="r3")
        # 重复提交不改变余额
        transfer(db, account, 200, "sell", tx_date="2025-06-03", request_id="r3")

        db.expire_all()
        account = db.query(AllowanceAccount).first()
        assert reconcile_account(db, account) == pytest.approx(0.0)
        assert float(account.current_balance) == pytest.approx(400.0)

    def test_clear_then_trade_keeps_ledger_consistent(self, db, seed):
        """清缴扣减与后续交易并存时，余额快照连续、账实一致。"""
        _add_activity(db, seed, seed["scope2"].id, 1000)  # 排放 = 1000 × 0.5703
        recalc_company_year(db, seed["company"].id, 2025)
        from app.services.calculation_service import annual_total as _total

        emission = _total(db, seed["company"].id, 2025)
        # 分配额恰好覆盖排放，清缴后余额归零
        allocate_quota(db, seed["company"].id, 2025, 1000, float(emission), 0)
        record = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert record.status == "compliant"

        account = db.query(AllowanceAccount).first()
        # 清缴后余额为 0，卖出必须被拒且不破坏一致性
        with pytest.raises(InsufficientBalanceError):
            transfer(db, account, 1, "sell", tx_date="2026-01-01", request_id="r-x")
        tx = transfer(db, account, 200, "buy", tx_date="2026-01-02", request_id="r-y")
        assert float(tx.balance_after) == pytest.approx(200.0)

        db.expire_all()
        account = db.query(AllowanceAccount).first()
        assert reconcile_account(db, account) == pytest.approx(0.0)
