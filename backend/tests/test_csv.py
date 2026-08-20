"""CSV import, field mapping, validation, dedup, suppression."""
from app.services import contacts as contact_svc
from app import models


def _make_campaign(db, owner_id=1):
    c = models.Campaign(owner_id=owner_id, name="Test", agent_mode="langgraph_only")
    db.add(c)
    db.commit()
    return c


def test_csv_import_mapping_and_validation(db):
    c = _make_campaign(db)
    csv = "Name,Email,Company\nAlice,alice@example.com,Acme\nBob,not-an-email,Co\n,missing@x.com,X"
    res = contact_svc.import_contacts(
        db, owner_id=1, campaign_id=c.id, csv_text=csv,
        field_map={"email": "Email", "first_name": "Name", "company": "Company"},
        has_header=True,
    )
    db.commit()
    assert res.total_rows == 3
    assert res.imported == 2  # alice + missing (valid email)
    assert res.invalid_email == 1  # bob
    emails = {x.email for x in db.query(models.Contact).all()}
    assert "alice@example.com" in emails


def test_csv_dedup(db):
    c = _make_campaign(db)
    csv = "Email\nalice@example.com\nalice@example.com\nbob@example.com"
    res = contact_svc.import_contacts(
        db, owner_id=1, campaign_id=c.id, csv_text=csv,
        field_map={"email": "Email"}, has_header=True,
    )
    db.commit()
    assert res.imported == 2
    assert res.duplicates_skipped == 1


def test_csv_suppression_filter(db):
    c = _make_campaign(db)
    db.add(models.Suppression(owner_id=1, email="blocked@example.com", reason="unsubscribe", source="csv"))
    db.commit()
    csv = "Email\nblocked@example.com\nok@example.com"
    res = contact_svc.import_contacts(
        db, owner_id=1, campaign_id=c.id, csv_text=csv,
        field_map={"email": "Email"}, has_header=True,
    )
    db.commit()
    assert res.suppressed_skipped == 1
    assert res.imported == 1
    assert "blocked@example.com" not in {x.email for x in db.query(models.Contact).all()}


def test_csv_positional_no_header(db):
    c = _make_campaign(db)
    csv = "alice@example.com,Alice\nbob@example.com,Bob"
    res = contact_svc.import_contacts(
        db, owner_id=1, campaign_id=c.id, csv_text=csv,
        field_map={"email": "0", "first_name": "1"}, has_header=False,
    )
    db.commit()
    assert res.imported == 2
