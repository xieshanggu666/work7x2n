from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import DATABASE_URL


def _configure_sqlite(dbapi_conn, _record):
    """SQLite 连接级配置：

    - foreign_keys=ON：开启外键约束，杜绝 account_id=0 之类的脏流水；
    - busy_timeout：写冲突时等待而不是立刻 database is locked；
    - 关闭 pysqlite 自动 BEGIN，由应用统一发起 BEGIN IMMEDIATE，
      确保一进入事务即持有 RESERVED 写锁，避免“先读后写”的 deferred
      事务在并发下升级锁失败（超额扣减/重复履约的根因之一）。
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()
    dbapi_conn.isolation_level = None


def create_app_engine(url: str, **kwargs):
    """创建带并发安全配置的引擎（正式库与文件型测试库共用）。"""
    if url.startswith("sqlite"):
        kwargs.setdefault("connect_args", {})["check_same_thread"] = False
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite)

        @event.listens_for(engine, "begin")
        def _begin_immediate(conn):  # noqa: ANN001
            # 每个写事务立即获取写锁，同一时刻只允许一个事务修改余额
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


engine = create_app_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
