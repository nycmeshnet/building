import logging

from .settings import TOKEN_ID_COOKIE, SESSION_COOKIE_DOMAIN
from django.utils.deprecation import MiddlewareMixin
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

log = logging.getLogger(__name__)

class TokenCookieMiddleWare(MiddlewareMixin):
    """
    Middleware to set token cookie
    If user is authenticated and there is no cookie, set the cookie,
    If the user is not authenticated and the cookie remains, delete it
    """

    def process_response(self, request, response):
        #if user and no cookie, set cookie
        if request.user.is_authenticated and not request.COOKIES.get(TOKEN_ID_COOKIE):
            response.set_cookie(TOKEN_ID_COOKIE, request.session['oidc_id_token'], domain=SESSION_COOKIE_DOMAIN,
            secure=True, httponly=True, samesite="Lax")

        elif not request.user.is_authenticated and request.COOKIES.get(TOKEN_ID_COOKIE):
            log.debug("Removing token cookie for logged-out user")
            #else if if no user and cookie remove user cookie, logout
            response.delete_cookie(TOKEN_ID_COOKIE, domain=SESSION_COOKIE_DOMAIN)
        return response

class OIDCAB(OIDCAuthenticationBackend):
    def create_user(self, claims):
        """Return object for a newly created user account."""
        username = claims.get('email')
        return self.UserModel.objects.create_user(username)

    def filter_users_by_claims(self, claims):
        """Return all users matching the specified username."""
        username = claims.get('email')
        if not username:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(username__iexact=username)
