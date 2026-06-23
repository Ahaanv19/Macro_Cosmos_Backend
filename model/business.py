"""
BusinessSubmission model.

This is an ADDITIVE feature: businesses (users with the "Business" role) submit
their details, which are stored here as `pending` until an Admin approves them.
Approved submissions are surfaced through the existing local-businesses system
(see api/businesses.py) without changing how the seeded businesses work.

Storage is persistent (SQLAlchemy) so submissions survive restarts, unlike the
in-memory seed list in api/businesses.py.
"""

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from __init__ import app, db

# Offset used when an approved submission is surfaced as a regular business, so
# its public id can never collide with the in-memory seed ids (1, 2, 3, ...).
APPROVED_BUSINESS_ID_OFFSET = 100000

VALID_STATUSES = ("pending", "approved", "rejected")


class BusinessSubmission(db.Model):
    """A business listing submitted by a Business-role user, pending Admin review."""

    __tablename__ = 'business_submissions'

    id = db.Column(db.Integer, primary_key=True)
    _name = db.Column(db.String(255), nullable=False)
    _description = db.Column(db.Text, default="")
    _address = db.Column(db.String(500), nullable=False)
    _website = db.Column(db.String(500), default="")
    _category = db.Column(db.String(120), nullable=False)
    _lat = db.Column(db.Float, nullable=True)
    _lng = db.Column(db.Float, nullable=True)
    _image = db.Column(db.String(500), default="")
    _status = db.Column(db.String(20), default="pending", nullable=False)
    # Owner reference (stored as plain values to avoid a hard FK dependency on
    # the users table name / migration ordering).
    _owner_id = db.Column(db.Integer, nullable=True)
    _owner_uid = db.Column(db.String(255), nullable=True)
    _review_note = db.Column(db.String(500), default="")
    _created_at = db.Column(db.DateTime, default=datetime.utcnow)
    _reviewed_at = db.Column(db.DateTime, nullable=True)

    def __init__(self, name, address, category, owner_id=None, owner_uid=None,
                 description="", website="", lat=None, lng=None, image=""):
        self._name = name
        self._address = address
        self._category = category
        self._description = description or ""
        self._website = website or ""
        self._lat = lat
        self._lng = lng
        self._image = image or ""
        self._status = "pending"
        self._owner_id = owner_id
        self._owner_uid = owner_uid
        self._review_note = ""

    # --- properties ---
    @property
    def status(self):
        return self._status

    @property
    def owner_id(self):
        return self._owner_id

    # --- persistence ---
    def create(self):
        """Persist a new submission. Returns self, or None on error."""
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except IntegrityError:
            db.session.rollback()
            return None

    def read(self):
        """Dictionary representation for API responses (full submission record)."""
        return {
            "id": self.id,
            "name": self._name,
            "description": self._description,
            "address": self._address,
            "website": self._website,
            "category": self._category,
            "lat": self._lat,
            "lng": self._lng,
            "image": self._image,
            "status": self._status,
            "owner_id": self._owner_id,
            "owner_uid": self._owner_uid,
            "review_note": self._review_note,
            "created_at": self._created_at.isoformat() if self._created_at else None,
            "reviewed_at": self._reviewed_at.isoformat() if self._reviewed_at else None,
        }

    def to_business(self):
        """
        Convert an approved submission into the same dict shape that
        api/businesses.py uses for its in-memory businesses, so existing
        endpoints can serve it with no special-casing.
        """
        return {
            "id": APPROVED_BUSINESS_ID_OFFSET + self.id,
            "name": self._name,
            "description": self._description or "",
            "address": self._address,
            "website": self._website or "",
            "image": self._image or "",
            "image_layout": "standard",
            "category": self._category,
            "lat": self._lat,
            "lng": self._lng,
            "created_at": self._created_at.isoformat() if self._created_at else datetime.utcnow().isoformat(),
            "is_active": True,
            # Marks the source so the UI/admin can distinguish community submissions.
            "submission_id": self.id,
        }

    def set_status(self, status, note=""):
        """Approve/reject a submission. Returns self, or None on error/invalid."""
        if status not in VALID_STATUSES:
            return None
        self._status = status
        self._review_note = note or ""
        self._reviewed_at = datetime.utcnow()
        try:
            db.session.commit()
            return self
        except IntegrityError:
            db.session.rollback()
            return None

    def delete(self):
        try:
            db.session.delete(self)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
        return None


def initBusinessSubmissions():
    """Create the table. Safe to call repeatedly; mirrors other model init funcs."""
    with app.app_context():
        db.create_all()
