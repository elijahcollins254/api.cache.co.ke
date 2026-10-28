"""
Custom middleware for handling CSRF and other API-specific logic.
"""
from django.middleware.csrf import CsrfViewMiddleware


class DisableCSRFForApiMiddleware(CsrfViewMiddleware):
    """
    Middleware that disables CSRF protection for API endpoints.
    Token-based authentication doesn't require CSRF tokens.
    """
    def process_view(self, request, view_func, view_args, view_kwargs):
        # Disable CSRF for all REST API endpoints that use token authentication
        api_paths = [
            '/users/',
            '/orders/',
            '/services/',
            '/notifications/',
            '/payments/',
            '/riders/',
            '/offers/',
            '/user/',
            '/loans/',
        ]
        
        for api_path in api_paths:
            if request.path.startswith(api_path):
                # Token authentication doesn't need CSRF protection
                return None
        
        # For other paths (like Django admin), apply normal CSRF checks
        return super().process_view(request, view_func, view_args, view_kwargs)
