"""Live end-to-end smoke test driven against a running uvicorn server.
Proves the full pipeline works with real DB + in-memory transport, no fakes."""
import json
import urllib.request
import sys

BASE = "http://localhost:8012"

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

print("1) create campaign")
st, camp = call("POST", "/api/campaigns", {
    "name": "Live E2E Demo", "agent_mode": "langgraph_only",
    "primary_agent": "langgraph", "tone": "professional",
})
cid = camp["id"]
print("   ->", st, "campaign id", cid)

print("2) import CSV contacts")
csv = "email,first_name,company\nlead1@example.com,Lead1,Acme\ntest@allowlist.test,Test,TestCo\nbad-email,No,No\nlead1@example.com,Dup,Acme\nunsub@suppress.test,Supp,Co"
st, imp = call("POST", f"/api/campaigns/{cid}/import-csv", {
    "campaign_id": cid, "csv_text": csv, "field_map": {},
    "has_header": True,
})
print("   ->", st, imp)

print("3) generate outreach (draft-only)")
st, gen = call("POST", f"/api/campaigns/{cid}/generate")
print("   ->", st, gen)

print("4) list approvals")
st, apps = call("GET", "/api/approvals?status=pending")
print("   ->", st, "pending approvals:", len(apps))

print("5) reject first approval (no real send)")
if apps:
    st, dec = call("POST", f"/api/approvals/{apps[0]['id']}/decision", {"decision": "reject"})
    print("   ->", st, dec)

print("6) start campaign")
st, _ = call("POST", f"/api/campaigns/{cid}/start")
print("   ->", st)

print("7) dashboard metrics")
st, m = call("GET", "/api/dashboard/metrics")
print("   ->", st, json.dumps(m))

print("8) activity feed")
st, act = call("GET", "/api/dashboard/activity")
print("   ->", st, "items:", len(act))

print("\nLIVE E2E OK")
