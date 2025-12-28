from flask import Blueprint, request, jsonify, g
from flask_restful import Api, Resource  # used for REST API building
from api.jwt_authorize import token_required
from api.subscription import requires_feature, get_feature_limit, get_user_tier
from model.savedLocations import SavedLocations

"""
This Blueprint object is used to define APIs for the Post model.
- Blueprint is used to modularize application files.
- This Blueprint is registered to the Flask app in main.py.
""" 
savedLocations_api = Blueprint('savedLocations', __name__, url_prefix='/api')

"""
The Api object is connected to the Blueprint object to define the API endpoints.
- The API object is used to add resources to the API.
- The objects added are mapped to code that contains the actions for the API.
- For more information, refer to the API docs: https://flask-restful.readthedocs.io/en/latest/api.html
"""
api = Api(savedLocations_api)

class SavedLocationsAPI:
    """
    Define the API CRUD endpoints for the Post model.
    There are four operations that correspond to common HTTP methods:
    - post: create a new post
    - get: read posts
    - put: update a post
    - delete: delete a post
    """
    class _CRUD(Resource):
        
        @token_required()
        @requires_feature('favorite_locations')
        def post(self):
            # Obtain the current user from the token
            current_user = g.current_user
            
            # Check feature limit (Plus users limited to 10 locations)
            limit = get_feature_limit(current_user, 'favorite_locations')
            if limit > 0:  # Not unlimited
                # Count user's existing locations
                existing_count = SavedLocations.query.filter_by(_user_id=current_user.id).count()
                if existing_count >= limit:
                    return jsonify({
                        'error': 'Location limit reached',
                        'message': f'You can save up to {limit} locations with your current plan',
                        'current_count': existing_count,
                        'limit': limit,
                        'tier': get_user_tier(current_user),
                        'upgrade_message': 'Upgrade to Pro for unlimited saved locations',
                        'upgrade_url': '/subscription'
                    }), 403
            
            # Obtain the request data sent by the RESTful client API
            data = request.get_json()
            # Create a new post object using the data from the request
            post = SavedLocations(current_user.id, current_user.name, data['address'], data['name'])
            # Save the post object using the ORM method defined in the model
            post.create()
            # Return response to the client in JSON format
            return jsonify(post.read())
        
        @token_required()
        def get(self):
            # Obtain the current user
            current_user = g.current_user
            # Find all the posts by the current user (only their own locations)
            posts = SavedLocations.query.filter_by(_user_id=current_user.id).all()
            # Prepare a JSON list of all the posts, uses for loop shortcut called list comprehension
            json_ready = [post.read() for post in posts]
            
            # Include limit info for frontend
            tier = get_user_tier(current_user)
            limit = get_feature_limit(current_user, 'favorite_locations')
            
            return jsonify({
                'locations': json_ready,
                'count': len(json_ready),
                'limit': limit,
                'unlimited': limit == -1,
                'tier': tier
            })
        
        @token_required()
        @requires_feature('favorite_locations')
        def put(self):
            """
            Update a section.
            """
            # Obtain the request data sent by the RESTful client API
            data = request.get_json()
            # Find the section to update
            updatedScoreData = SavedLocations.query.get(data['id'])
            # Save the section object using the Object Relational Mapper (ORM) method defined in the model
            updatedScoreData.update({'user_address': data['address'], 'user_name': data['name']})
            # Return a JSON restful response to the client
            return jsonify(updatedScoreData.read())

        @token_required()
        @requires_feature('favorite_locations')
        def delete(self):
            # Obtain the current user
            current_user = g.current_user
            # Obtain the request data
            data = request.get_json()
            # Find the current post from the database table(s)
            post = SavedLocations.query.get(data['id'])
            # Delete the post using the ORM method defined in the model
            post.delete()
            # Return response
            return jsonify({"message": "Post deleted"})

    """
    Map the _CRUD class to the API endpoints for /post.
    - The API resource class inherits from flask_restful.Resource.
    - The _CRUD class defines the HTTP methods for the API.
    """
    api.add_resource(_CRUD, '/saved_locations')