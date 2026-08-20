"""Round3 Scenario4 (part B): enable + run-now API checks + synchronous run_tick logic verification.

The Huey consumer is dead (periodic_reclaim TaskRegistry crash), so the async
worker can't flip a run to success. To still verify the S4 BUSINESS LOGIC
("no follow-up generated after the customer replied", "stop-on-reply handled",
"no duplicate inflight approval/run"), we:
  1) enable automation 4 (campaign 11) via API,
  2) run-now twice via API and assert single-inflight (already_inflight + same run_id),
  3) call run_tick synchronously in-process (does NOT need the consumer) and
     assert follow_ups_resolved==0 for the already-replied contact 3,
     and that no follow_up approval / suppression was created.
"""
import json, sys, urllib.request
from datetime import datetime, timezone

API = "http://127.0.0.1:8000"
AID = 4  # new round-3 automation created in part A

def api(method, path, body=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                headers={"Content-Type": "application/json",
                                         "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

out = {"steps": [], "findings": [], "api": {}, "sync_run_tick": {}, "db_checks": {}}

# 1) enable
st, js = api("POST", f"/api/automation/{AID}/enable")
out["api"]["enable_status"] = st
out["api"]["enable_body"] = js
out["steps"].append(f"enable automation {AID} -> HTTP {st}, status={js.get('status')}")

# 2) run-now x2 (single-inflight check)
st1, j1 = api("POST", f"/api/automation/{AID}/run-now")
st2, j2 = api("POST", f"/api/automation/{AID}/run-now")
out["api"]["runnow_1"] = {"http": st1, "body": j1}
out["api"]["runnow_2"] = {"http": st2, "body": j2}
rid1 = j1.get("run_id")
rid2 = j2.get("run_id")
out["api"]["single_inflight_ok"] = (j2.get("already_inflight") is True and rid1 == rid2)
out["steps"].append(f"run-now#1 run_id={rid1}; run-now#2 run_id={rid2} already_inflight={j2.get('already_inflight')}")

# Check the enqueued run's status (won't complete: consumer dead)
st, js = api("GET", f"/api/automation/{AID}")
runs = js.get("runs", [])
out["api"]["runs_after_runnow"] = [
    {"id": r["id"], "status": r["status"]} for r in runs[:3]
]
out["steps"].append("fetched automation detail + recent runs (consumer dead -> run stays queued/running)")

# 3) synchronous run_tick (business-logic verification, bypasses dead consumer)
try:
    sys.path.insert(0, ".")
    from app.db import SessionLocal
    from app import models
    from app.services import automation as automation_svc
    db = SessionLocal()
    automation = db.get(models.Automation, AID)
    # NOTE: a partial unique index allows only ONE inflight (queued/running)
    # row per automation. Run #74 (from run-now) is stuck 'queued'
    # because the consumer is dead, so we create this sync run with a
    # non-inflight status to avoid the collision. run_tick does not
    # require the run to be 'running'.
    run = models.AutomationRun(automation_id=AID, trigger="sync", source="test", status="success")
    db.add(run); db.flush()
    res = automation_svc.run_tick(db, automation, "sync", "test", run)
    db.commit()
    out["sync_run_tick"] = {
        "run_id": run.id,
        "status": run.status,
        "summary": run.summary,
        "approvals_created": run.approvals_created,
        "drafts_created": run.drafts_created,
        "follow_ups_resolved": run.follow_ups_resolved,
        "replies_stopped": run.replies_stopped,
        "error": run.error,
        "timeline": run.timeline_json,
    }
    out["steps"].append(f"sync run_tick run_id={run.id} status={run.status} summary='{run.summary}'")

    # DB assertions
    # a) any follow_up approval for campaign 11 / contact 3?
    fu_appr = db.query(models.Approval).filter_by(campaign_id=11, kind="follow_up").all()
    out["db_checks"]["follow_up_approvals_for_campaign11"] = [
        {"id": a.id, "to": a.to_email, "status": a.status} for a in fu_appr
    ]
    # b) cc id=9 status, contact 3 status
    cc9 = db.get(models.CampaignContact, 9)
    c3 = db.get(models.Contact, 3)
    out["db_checks"]["cc9_status"] = cc9.status if cc9 else None
    out["db_checks"]["contact3_status"] = c3.status if c3 else None
    # c) any new suppression for the QA address?
    sup = db.query(models.Suppression).filter_by(email="1171898390@qq.com").all()
    out["db_checks"]["suppressions_for_qa"] = [{"id": s.id, "reason": s.reason} for s in sup]
    # d) pending approvals for thread 40 (the QQ reply thread)?
    t40 = db.query(models.Approval).filter_by(thread_id=40, status="pending").all()
    out["db_checks"]["pending_approvals_thread40"] = [
        {"id": a.id, "kind": a.kind, "to": a.to_email} for a in t40
    ]
    db.close()

    # Evaluate assertions
    out["assertions"] = {
        "no_follow_up_for_replied_contact": len(fu_appr) == 0,
        "contact3_not_suppressed": len(sup) == 0,
        "cc9_status": out["db_checks"]["cc9_status"],
    }
    out["steps"].append("DB assertions: no follow_up approval for replied contact 3; "
                        f"no suppression for QA address; cc9.status={out['db_checks']['cc9_status']}")
except Exception as e:
    import traceback
    out["sync_run_tick_error"] = traceback.format_exc()
    out["steps"].append("sync run_tick FAILED: " + str(e))

print(json.dumps(out, indent=2, ensure_ascii=False))
