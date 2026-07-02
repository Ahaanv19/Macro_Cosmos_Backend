from flask import Blueprint, request, jsonify, g
from datetime import datetime
from api.jwt_authorize import token_required
from model.business import BusinessSubmission, APPROVED_BUSINESS_ID_OFFSET
from model.user import User

# Reuse the app-wide rate limiter for account-creation throttling. Degrade to a
# no-op decorator if the security module isn't available so nothing breaks.
try:
    from utils.security import limiter
except Exception:  # pragma: no cover
    class _NoLimit:
        def limit(self, *a, **k):
            def deco(f):
                return f
            return deco
    limiter = _NoLimit()

# Reuse the central XSS sanitizer; fall back to a minimal escaper if unavailable.
try:
    from utils.security import sanitize_text
except Exception:  # pragma: no cover
    def sanitize_text(value, max_length=None):
        if not isinstance(value, str):
            return value
        cleaned = value.replace('<', '&lt;').replace('>', '&gt;').strip()
        return cleaned[:max_length] if max_length else cleaned

businesses_api = Blueprint('businesses_api', __name__, url_prefix='/api')

# In-memory storage for businesses (seeded with initial data)
businesses = [
    {
        "id": 1,
        "name": "ActiveMed Integrative Health Center",
        "description": "We believe in a collaborative approach to healthcare. We offer acupuncture, massage therapy, functional medicine, physical therapy, and axon therapy.",
        "address": "11588 Via Rancho San Diego, Suite 101, El Cajon, CA 92019",
        "website": "https://activemedhealth.com/",
        "image": "bus.png",
        "image_layout": "standard",
        "category": "Healthcare",
        "lat": 32.7914,
        "lng": -116.9259,
        "created_at": datetime.utcnow().isoformat(),
        "is_active": True
    },
    {
        "id": 2,
        "name": "Digital One Printing",
        "description": "Digital One Printing is your premier one-stop Poway printshop that offers a wide range of services, has many years of experience and a tremendous reputation. Digital, Offset, Large Format, Posters, Banners, Trade show graphics, Signs, Promotional Products, Bindery and more.",
        "address": "12630 Poway Rd, Poway, CA 92064",
        "website": "https://d1printing.net/",
        "image": "Screenshot 2025-07-23 at 8.34.48 AM.png",
        "image_layout": "wide",
        "category": "Printing Services",
        "lat": 32.9579,
        "lng": -117.0287,
        "created_at": datetime.utcnow().isoformat(),
        "is_active": True
    }
]

# In-memory storage for user spotlights (user_id -> set of business_ids)
user_spotlights = {}

# Counter for new business IDs
business_id_counter = 3


def format_business_response(business):
    """Format a business for API response with coordinates object."""
    return {
        "id": business["id"],
        "name": business["name"],
        "description": business["description"],
        "address": business["address"],
        "website": business["website"],
        "image": business["image"],
        "imageLayout": business.get("image_layout", "standard"),
        "category": business["category"],
        "coordinates": {
            "lat": business["lat"],
            "lng": business["lng"]
        }
    }


def format_business_minimal(business):
    """Format a business with minimal data for spotlight/map display."""
    return {
        "id": business["id"],
        "name": business["name"],
        "address": business["address"],
        "category": business["category"],
        "coordinates": {
            "lat": business["lat"],
            "lng": business["lng"]
        },
        "website": business["website"]
    }


@businesses_api.route('/businesses', methods=['GET'])
def get_businesses():
    """
    Get all active local businesses.
    
    This endpoint is PUBLIC (no authentication required).
    
    Response:
        [
            {
                "id": 1,
                "name": "ActiveMed Integrative Health Center",
                "description": "We believe in a collaborative approach to healthcare...",
                "address": "11588 Via Rancho San Diego, Suite 101, El Cajon, CA 92019",
                "website": "https://activemedhealth.com/",
                "image": "bus.png",
                "imageLayout": "standard",
                "category": "Healthcare",
                "coordinates": { "lat": 32.7914, "lng": -116.9259 }
            },
            ...
        ]
    """
    active_businesses = [b for b in businesses if b.get("is_active", True)]
    return jsonify([format_business_response(b) for b in active_businesses])


@businesses_api.route('/businesses/<int:business_id>', methods=['GET'])
def get_business(business_id):
    """
    Get a single business by ID.
    
    This endpoint is PUBLIC (no authentication required).
    
    Response:
        {
            "id": 1,
            "name": "ActiveMed Integrative Health Center",
            ...
        }
    """
    for business in businesses:
        if business["id"] == business_id and business.get("is_active", True):
            return jsonify(format_business_response(business))
    
    return jsonify({"error": "Business not found"}), 404


@businesses_api.route('/businesses/spotlight', methods=['GET'])
@token_required()
def get_user_spotlights():
    """
    Get the current user's spotlighted business IDs.
    
    This endpoint REQUIRES authentication.
    
    Response:
        {
            "spotlighted_ids": [1, 2]
        }
    """
    current_user = g.current_user
    user_id = current_user.id
    
    # Get user's spotlighted business IDs
    spotlighted_ids = list(user_spotlights.get(user_id, set()))
    
    return jsonify({
        "spotlighted_ids": spotlighted_ids
    })


@businesses_api.route('/businesses/spotlight', methods=['POST'])
@token_required()
def toggle_spotlight():
    """
    Toggle spotlight status for a business.
    
    This endpoint REQUIRES authentication.
    
    Request Body:
        {
            "business_id": 1,
            "spotlight": true
        }
    
    Response:
        {
            "success": true,
            "business_id": 1,
            "spotlight": true
        }
    """
    current_user = g.current_user
    user_id = current_user.id
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    
    business_id = data.get("business_id")
    spotlight = data.get("spotlight")
    
    if business_id is None:
        return jsonify({"error": "business_id is required"}), 400
    
    if spotlight is None:
        return jsonify({"error": "spotlight (boolean) is required"}), 400
    
    # Verify business exists
    business_exists = any(b["id"] == business_id and b.get("is_active", True) for b in businesses)
    if not business_exists:
        return jsonify({"error": "Business not found"}), 404
    
    # Initialize user's spotlight set if needed
    if user_id not in user_spotlights:
        user_spotlights[user_id] = set()
    
    # Toggle spotlight
    if spotlight:
        user_spotlights[user_id].add(business_id)
    else:
        user_spotlights[user_id].discard(business_id)
    
    return jsonify({
        "success": True,
        "business_id": business_id,
        "spotlight": spotlight
    })


@businesses_api.route('/businesses/spotlight/sync', methods=['POST'])
@token_required()
def sync_spotlights():
    """
    Sync localStorage spotlights with the server.
    
    This endpoint is called when a user logs in to merge their
    localStorage spotlights with their server-side spotlights.
    
    This endpoint REQUIRES authentication.
    
    Request Body:
        {
            "spotlighted_ids": [1, 2, 3]
        }
    
    Response:
        {
            "success": true,
            "spotlighted_ids": [1, 2, 3, 4]  // merged list
        }
    """
    current_user = g.current_user
    user_id = current_user.id
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    
    local_ids = data.get("spotlighted_ids", [])
    
    # Initialize user's spotlight set if needed
    if user_id not in user_spotlights:
        user_spotlights[user_id] = set()
    
    # Merge local IDs with server IDs
    for bid in local_ids:
        # Only add if business exists and is active
        business_exists = any(b["id"] == bid and b.get("is_active", True) for b in businesses)
        if business_exists:
            user_spotlights[user_id].add(bid)
    
    # Return merged list
    return jsonify({
        "success": True,
        "spotlighted_ids": list(user_spotlights[user_id])
    })


@businesses_api.route('/businesses/spotlight/all', methods=['GET'])
@token_required()
def get_spotlighted_businesses():
    """
    Get full business data for all spotlighted businesses (for map display).
    
    This endpoint REQUIRES authentication.
    
    Response:
        [
            {
                "id": 1,
                "name": "ActiveMed Integrative Health Center",
                "address": "11588 Via Rancho San Diego, Suite 101, El Cajon, CA 92019",
                "category": "Healthcare",
                "coordinates": { "lat": 32.7914, "lng": -116.9259 },
                "website": "https://activemedhealth.com/"
            },
            ...
        ]
    """
    current_user = g.current_user
    user_id = current_user.id
    
    # Get user's spotlighted business IDs
    spotlighted_ids = user_spotlights.get(user_id, set())
    
    # Get full business data for spotlighted businesses
    spotlighted_businesses = [
        format_business_minimal(b) 
        for b in businesses 
        if b["id"] in spotlighted_ids and b.get("is_active", True)
    ]
    
    return jsonify(spotlighted_businesses)


# Admin endpoints for managing businesses (optional)

@businesses_api.route('/businesses', methods=['POST'])
@token_required(roles=["Admin"])
def create_business():
    """
    Create a new business (Admin only).
    
    Request Body:
        {
            "name": "Business Name",
            "description": "Description",
            "address": "123 Main St",
            "website": "https://example.com",
            "image": "image.png",
            "image_layout": "standard",
            "category": "Category",
            "lat": 32.7157,
            "lng": -117.1611
        }
    
    Response:
        {
            "message": "Business created successfully",
            "business": { ...business data... }
        }
    """
    global business_id_counter
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    
    # Validate required fields
    required_fields = ["name", "address", "category", "lat", "lng"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400
    
    # Create the business
    business = {
        "id": business_id_counter,
        "name": data["name"],
        "description": data.get("description", ""),
        "address": data["address"],
        "website": data.get("website", ""),
        "image": data.get("image", ""),
        "image_layout": data.get("image_layout", "standard"),
        "category": data["category"],
        "lat": data["lat"],
        "lng": data["lng"],
        "created_at": datetime.utcnow().isoformat(),
        "is_active": True
    }
    
    businesses.append(business)
    business_id_counter += 1
    
    return jsonify({
        "message": "Business created successfully",
        "business": format_business_response(business)
    }), 201


@businesses_api.route('/businesses/<int:business_id>', methods=['PUT'])
@token_required(roles=["Admin"])
def update_business(business_id):
    """
    Update a business (Admin only).
    
    Request Body:
        {
            "name": "Updated Name",
            ...other fields to update...
        }
    
    Response:
        {
            "message": "Business updated successfully",
            "business": { ...updated business data... }
        }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    
    # Find the business
    for business in businesses:
        if business["id"] == business_id:
            # Update allowed fields
            allowed_fields = ["name", "description", "address", "website", "image", "image_layout", "category", "lat", "lng", "is_active"]
            for field in allowed_fields:
                if field in data:
                    business[field] = data[field]
            
            return jsonify({
                "message": "Business updated successfully",
                "business": format_business_response(business)
            })
    
    return jsonify({"error": "Business not found"}), 404


@businesses_api.route('/businesses/<int:business_id>', methods=['DELETE'])
@token_required(roles=["Admin"])
def delete_business(business_id):
    """
    Delete a business (Admin only).
    
    This performs a soft delete by setting is_active to False.
    
    Response:
        {
            "message": "Business deleted successfully"
        }
    """
    for business in businesses:
        if business["id"] == business_id:
            business["is_active"] = False

            # Remove from all user spotlights
            for user_id in user_spotlights:
                user_spotlights[user_id].discard(business_id)

            return jsonify({"message": "Business deleted successfully"})

    return jsonify({"error": "Business not found"}), 404


# =====================================================================
# BUSINESS ACCOUNTS & SUBMISSIONS (additive)
# ---------------------------------------------------------------------
# Businesses create a "Business"-role account via the existing user/login
# system, submit their listing for review, and an Admin approves it. Approved
# submissions are appended to the in-memory `businesses` list above, so every
# existing endpoint (get_businesses, spotlight, map) serves them unchanged.
# Submissions are persisted in the BusinessSubmission table (survive restarts).
# =====================================================================

def load_approved_into_memory():
    """
    Append approved submissions to the in-memory businesses list. Idempotent and
    best-effort — called at startup so approved listings survive restarts.
    """
    try:
        existing_ids = {b["id"] for b in businesses}
        approved = BusinessSubmission.query.filter_by(_status="approved").all()
        for sub in approved:
            biz = sub.to_business()
            if biz["id"] not in existing_ids:
                businesses.append(biz)
                existing_ids.add(biz["id"])
    except Exception as e:  # table may not exist yet on a fresh DB
        print(f"load_approved_into_memory: {e}")


@businesses_api.route('/business/register', methods=['POST'])
@limiter.limit("5 per minute; 20 per hour; 50 per day")
def register_business_account():
    """
    Create a Business-role account using the existing User model + security
    (password hashing, JWT login via /api/authenticate). PUBLIC endpoint.

    The role is forced to "Business" server-side, so a client cannot escalate
    to Admin. Business accounts can submit listings, which still require Admin
    approval before going live.
    """
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    uid = (data.get('uid') or '').strip()
    password = data.get('password') or ''

    if len(name) < 2:
        return jsonify({"error": "Business name must be at least 2 characters"}), 400
    if len(uid) < 2:
        return jsonify({"error": "Username must be at least 2 characters"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    user = User(name=name, uid=uid, password=password, role='Business')
    if not user.create():
        return jsonify({"error": f"Username '{uid}' is already taken"}), 400

    return jsonify({"message": "Business account created. You can now log in.", "uid": uid}), 201


@businesses_api.route('/business/submit', methods=['POST'])
@token_required(roles=["Business", "Admin"])
def submit_business():
    """A Business-role user submits a listing for Admin approval (status=pending)."""
    user = g.current_user
    data = request.get_json(silent=True) or {}

    name = sanitize_text((data.get('name') or '').strip(), max_length=255)
    address = sanitize_text((data.get('address') or '').strip(), max_length=500)
    category = sanitize_text((data.get('category') or '').strip(), max_length=120)
    description = sanitize_text((data.get('description') or '').strip(), max_length=2000)
    website = (data.get('website') or '').strip()
    image = sanitize_text((data.get('image') or '').strip(), max_length=500)

    missing = [label for label, val in (('name', name), ('address', address), ('category', category)) if not val]
    if missing:
        return jsonify({"error": f"Missing required field(s): {', '.join(missing)}"}), 400

    # Normalize website to a safe http(s) URL or drop it.
    if website:
        if not (website.startswith('http://') or website.startswith('https://')):
            website = 'https://' + website
        if len(website) > 500:
            website = website[:500]

    lat = data.get('lat')
    lng = data.get('lng')
    try:
        lat = float(lat) if lat not in (None, '') else None
        lng = float(lng) if lng not in (None, '') else None
    except (ValueError, TypeError):
        lat, lng = None, None

    sub = BusinessSubmission(
        name=name, address=address, category=category,
        owner_id=user.id, owner_uid=user.uid, description=description,
        website=website, lat=lat, lng=lng, image=image,
    )
    if not sub.create():
        return jsonify({"error": "Could not save submission"}), 500

    return jsonify({
        "message": "Submission received. It will appear in Local Businesses once an admin approves it.",
        "submission": sub.read(),
    }), 201


@businesses_api.route('/business/mine', methods=['GET'])
@token_required(roles=["Business", "Admin"])
def my_business_submissions():
    """List the current Business user's own submissions (any status)."""
    user = g.current_user
    subs = (BusinessSubmission.query
            .filter_by(_owner_id=user.id)
            .order_by(BusinessSubmission.id.desc())
            .all())
    return jsonify([s.read() for s in subs])


@businesses_api.route('/business/submissions', methods=['GET'])
@token_required(roles=["Admin"])
def list_business_submissions():
    """Admin: list submissions, optionally filtered by ?status=pending|approved|rejected."""
    status = request.args.get('status')
    query = BusinessSubmission.query
    if status in ("pending", "approved", "rejected"):
        query = query.filter_by(_status=status)
    subs = query.order_by(BusinessSubmission.id.desc()).all()
    return jsonify([s.read() for s in subs])


@businesses_api.route('/business/submissions/<int:submission_id>/approve', methods=['POST'])
@token_required(roles=["Admin"])
def approve_business_submission(submission_id):
    """Admin: approve a submission and publish it to Local Businesses immediately."""
    sub = BusinessSubmission.query.get(submission_id)
    if not sub:
        return jsonify({"error": "Submission not found"}), 404
    if not sub.set_status("approved"):
        return jsonify({"error": "Could not approve submission"}), 500

    # Surface immediately via the existing in-memory list (no restart needed).
    biz = sub.to_business()
    if not any(b["id"] == biz["id"] for b in businesses):
        businesses.append(biz)

    return jsonify({"message": "Submission approved and published.", "submission": sub.read()})


@businesses_api.route('/business/submissions/<int:submission_id>/reject', methods=['POST'])
@token_required(roles=["Admin"])
def reject_business_submission(submission_id):
    """Admin: reject a submission (optionally with a note). Unpublishes if it was live."""
    data = request.get_json(silent=True) or {}
    note = sanitize_text((data.get('note') or '').strip(), max_length=500)

    sub = BusinessSubmission.query.get(submission_id)
    if not sub:
        return jsonify({"error": "Submission not found"}), 404
    if not sub.set_status("rejected", note=note):
        return jsonify({"error": "Could not reject submission"}), 500

    # If it had been approved/published, remove it from the public in-memory list.
    biz_id = APPROVED_BUSINESS_ID_OFFSET + sub.id
    businesses[:] = [b for b in businesses if b["id"] != biz_id]

    return jsonify({"message": "Submission rejected.", "submission": sub.read()})
