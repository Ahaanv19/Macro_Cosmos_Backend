"""
Database-backed profile picture storage.

Profile pictures were stored only as files under instance/uploads. On the
deployed backend that directory is ephemeral (container filesystem) and/or not
shared across instances, so an uploaded picture vanishes on reload even though
the DB kept the filename — producing "it uploads but doesn't save."

This stores the image (base64) in the database, which persists and is shared
across instances, so pictures survive reloads and restarts. It's a separate
self-creating table (same pattern as UserMFA / UserPasskey), so the existing
users table and all its queries are completely untouched. The old file-based
path is kept as a fallback for any pre-existing pictures.
"""

from __init__ import db


class UserProfilePicture(db.Model):
    __tablename__ = 'user_profile_pictures'

    id = db.Column(db.Integer, primary_key=True)
    _user_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    _data = db.Column(db.Text, nullable=True)  # base64-encoded image (no data: prefix)

    def __init__(self, user_id, data):
        self._user_id = user_id
        self._data = data

    @staticmethod
    def get_data(user_id):
        """Return the stored base64 image for a user, or None."""
        row = UserProfilePicture.query.filter_by(_user_id=user_id).first()
        return row._data if row and row._data else None

    @staticmethod
    def set_data(user_id, data):
        """Insert or update the user's profile picture (base64). Commits."""
        row = UserProfilePicture.query.filter_by(_user_id=user_id).first()
        if row:
            row._data = data
        else:
            row = UserProfilePicture(user_id=user_id, data=data)
            db.session.add(row)
        db.session.commit()
        return row

    @staticmethod
    def clear_data(user_id):
        """Remove the user's stored profile picture, if any. Commits."""
        row = UserProfilePicture.query.filter_by(_user_id=user_id).first()
        if row:
            db.session.delete(row)
            db.session.commit()


def initUserProfilePictures():
    """No seed data needed; table is created at startup."""
    pass
