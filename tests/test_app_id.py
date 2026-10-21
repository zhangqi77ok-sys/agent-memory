from app import models as m


def test_new_rows_dual_write_app_id_and_tenant_id(db):
    f = m.MemoryFact(user_id="alice", subject="s", content="c")
    a = m.AuditLog(user_id="alice", action="recall", fact_ids=[])
    db.add_all([f, a])
    db.commit()
    db.refresh(f)
    db.refresh(a)
    assert (f.app_id, f.tenant_id) == ("tiancode", "tiancode")
    assert (a.app_id, a.tenant_id) == ("tiancode", "tiancode")


def test_parallel_constraints_exist():
    names = {c.name for c in m.MemoryJob.__table__.constraints} | {c.name for c in m.UserSetting.__table__.constraints}
    assert {"uq_job_idem", "uq_job_idem_app", "uq_user_setting", "uq_user_setting_app"} <= names
    assert {"ix_fact_user_subject", "ix_fact_app_user_subject"} <= {i.name for i in m.MemoryFact.__table__.indexes}
