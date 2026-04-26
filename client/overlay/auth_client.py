"""Client-side authentication — OAuth via Supabase + server JWT management."""

from __future__ import annotations

import http.server
import json
import logging
import socket
import threading
import time
import urllib.parse
import webbrowser
from base64 import urlsafe_b64decode
from dataclasses import dataclass

import httpx

from client.overlay.env import ClientEnv, clear_client_tokens, save_client_tokens

logger = logging.getLogger(__name__)


def _read_jwt_claims(token: str) -> dict:
    """Decode JWT payload without cryptographic verification.

    Returns an empty dict if the token is malformed.
    """
    try:
        payload_b64 = token.split(".")[1]
        # Pad to a multiple of 4
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


@dataclass
class ServerSession:
    """Active session with the NemeDraft server."""

    token: str
    expires_at: int
    email: str
    is_vip: bool = False

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - 30  # 30s buffer


class AuthClient:
    """Handles Supabase OAuth and server JWT lifecycle.

    Tokens are persisted to ``.env.client.local``.  On restart the
    client calls :meth:`try_auto_login` to silently restore the session.
    """

    def __init__(self, env: ClientEnv, arena_player_id: str = "") -> None:
        self._env = env
        self._server_base = f"{env.server_url}:{env.server_port}"
        self._supabase_url = env.supabase_url
        self._supabase_anon_key = env.supabase_anon_key
        self._supabase_refresh_token: str = env.saved_supabase_refresh_token
        self._arena_player_id = arena_player_id
        self.session: ServerSession | None = None

        # Used to cancel an in-progress OAuth flow
        self._cancel_event = threading.Event()
        self._oauth_httpd: http.server.HTTPServer | None = None

        # Restore saved server token if present — but only when we have
        # an arena player ID.  Without one the server cannot verify the
        # user so we must not pretend the session is valid.
        if env.saved_server_token and arena_player_id:
            self._restore_saved_session()
            logger.info(
                "Restored session from stored token (email=%s, is_vip=%s, expired=%s)",
                self.session.email, self.session.is_vip, self.session.is_expired,
            )

    def set_arena_player_id(self, arena_player_id: str) -> None:
        """Set the Arena player ID after startup.

        Args:
            arena_player_id: Stable Arena account ID read from memory.

        Returns:
            None.
        """
        self._arena_player_id = arena_player_id
        if self.session is None and self._env.saved_server_token and arena_player_id:
            self._restore_saved_session()

    # ------------------------------------------------------------------
    # OAuth login (opens browser)
    # ------------------------------------------------------------------

    def login_google(self) -> ServerSession | None:
        """Initiate Google OAuth via Supabase — opens the system browser."""
        return self._oauth_login("google")

    def login_microsoft(self) -> ServerSession | None:
        """Initiate Microsoft OAuth via Supabase — opens the system browser."""
        return self._oauth_login("azure")

    def _oauth_login(self, provider: str) -> ServerSession | None:
        """Run the full OAuth flow for *provider*.

        1. Start a temporary local HTTP server on a random port.
        2. Open Supabase OAuth URL in the system browser.
        3. Wait for the redirect callback with the tokens.
        4. Exchange the Supabase access token for a server JWT.

        The flow can be cancelled via :meth:`cancel_login`.
        """
        self._cancel_event.clear()

        # Find a free port
        callback_port = self._find_free_port()
        redirect_uri = f"http://localhost:{callback_port}/callback"

        # Build Supabase OAuth URL
        auth_url = (
            f"{self._supabase_url}/auth/v1/authorize?"
            f"provider={provider}&"
            f"redirect_to={urllib.parse.quote(redirect_uri)}"
        )

        # Azure requires the 'email' scope for Supabase Auth to receive
        # a valid email address from Microsoft Entra ID.
        if provider == "azure":
            auth_url += "&scopes=email"

        # Holder for the result from the callback handler
        result: dict = {}
        callback_received = threading.Event()

        class _CallbackHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                """Handle the OAuth redirect callback."""
                parsed = urllib.parse.urlparse(self.path)

                if parsed.path == "/callback":
                    # Supabase sends tokens as URL fragment (#access_token=...)
                    # which the browser doesn't forward to the server.  Serve a
                    # small page that extracts the fragment and posts it back.
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"""<!DOCTYPE html><html><body>
                    <p id="status">Completing login...</p>
                    <script>
                    const h = window.location.hash.substring(1);
                    const params = h || window.location.search.substring(1);
                    if (params) {
                        fetch('/token?' + params).then(() => {
                            document.getElementById('status').textContent =
                                'You are logged in! You can close this page.';
                        });
                    } else {
                        document.getElementById('status').textContent =
                            'Login failed - no tokens received.';
                    }
                    </script></body></html>""")
                    return

                if parsed.path == "/token":
                    params = urllib.parse.parse_qs(parsed.query)
                    result["access_token"] = params.get("access_token", [""])[0]
                    result["refresh_token"] = params.get("refresh_token", [""])[0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"OK")
                    callback_received.set()
                    return

                self.send_response(404)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass  # Suppress HTTP logging

        httpd = http.server.HTTPServer(("127.0.0.1", callback_port), _CallbackHandler)
        httpd.timeout = 1
        self._oauth_httpd = httpd

        def _serve() -> None:
            while not callback_received.is_set() and not self._cancel_event.is_set():
                httpd.handle_request()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()

        logger.info("Opening browser for %s OAuth (port %d)", provider, callback_port)
        webbrowser.open(auth_url)

        # Wait up to 120s, checking cancel every second
        deadline = time.monotonic() + 120
        while not callback_received.is_set():
            if self._cancel_event.is_set():
                logger.info("OAuth login cancelled by user")
                httpd.server_close()
                self._oauth_httpd = None
                return None
            if time.monotonic() >= deadline:
                logger.warning("OAuth timed out after 120s")
                httpd.server_close()
                self._oauth_httpd = None
                return None
            callback_received.wait(timeout=1)

        httpd.server_close()
        self._oauth_httpd = None

        access_token = result.get("access_token", "")
        refresh_token = result.get("refresh_token", "")
        if not access_token:
            logger.error("No access token received from OAuth callback")
            return None

        self._supabase_refresh_token = refresh_token
        return self._exchange_for_server_token(access_token)

    def cancel_login(self) -> None:
        """Cancel any in-progress OAuth flow."""
        self._cancel_event.set()
        if self._oauth_httpd:
            try:
                self._oauth_httpd.server_close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Token exchange & refresh
    # ------------------------------------------------------------------

    def _exchange_for_server_token(self, supabase_token: str) -> ServerSession | None:
        """Call POST /api/login to exchange a Supabase token for a server JWT."""
        try:
            body: dict[str, str] = {"supabase_token": supabase_token}
            if self._arena_player_id:
                body["arena_player_id"] = self._arena_player_id
            resp = httpx.post(
                f"{self._server_base}/api/login",
                json=body,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error("Server login failed: %d %s", resp.status_code, resp.text)
                return None
            data = resp.json()
            session = ServerSession(
                token=data["token"],
                expires_at=data["expires_at"],
                email=data.get("email", ""),
                is_vip=data.get("is_vip", False),
            )
            self.session = session
            self._save()
            logger.info("Server session established (email=%s, is_vip=%s)", session.email, session.is_vip)
            return session
        except httpx.HTTPError:
            logger.exception("Failed to exchange token with server")
            return None

    def refresh(self) -> ServerSession | None:
        """Refresh the session using the stored Supabase refresh token."""
        if not self._supabase_refresh_token:
            logger.warning("No Supabase refresh token available — cannot refresh session")
            return None

        if not self._supabase_anon_key:
            logger.warning("No Supabase anon key configured — cannot refresh session")
            return None

        # Step 1: refresh the Supabase token
        try:
            resp = httpx.post(
                f"{self._supabase_url}/auth/v1/token?grant_type=refresh_token",
                json={"refresh_token": self._supabase_refresh_token},
                headers={
                    "apikey": self._supabase_anon_key,
                    "Authorization": f"Bearer {self._supabase_anon_key}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error("Supabase refresh failed: %d %s", resp.status_code, resp.text[:100])
                return None
            data = resp.json()
            new_access = data.get("access_token", "")
            new_refresh = data.get("refresh_token", self._supabase_refresh_token)
            self._supabase_refresh_token = new_refresh
        except httpx.HTTPError:
            logger.exception("Supabase refresh request failed")
            return None

        if not new_access:
            return None

        # Step 2: exchange new Supabase token for server JWT
        return self._exchange_for_server_token(new_access)

    # ------------------------------------------------------------------
    # Auto-login / logout
    # ------------------------------------------------------------------

    def try_auto_login(self) -> bool:
        """Attempt to restore the session from stored tokens.

        Returns True if the session is valid or was successfully refreshed.
        """
        if self.session and not self.session.is_expired:
            return True

        # Token expired or missing — try refresh
        session = self.refresh()
        return session is not None

    def logout(self) -> None:
        """Clear all stored tokens and the active session."""
        self.session = None
        self._supabase_refresh_token = ""
        clear_client_tokens()
        logger.info("User logged out — tokens cleared")

    @property
    def is_authenticated(self) -> bool:
        return self.session is not None and not self.session.is_expired

    @property
    def user_email(self) -> str:
        return self.session.email if self.session else ""

    def get_token(self) -> str | None:
        """Return a valid server JWT, or None."""
        if not self.session:
            return None
        if self.session.is_expired:
            refreshed = self.refresh()
            if not refreshed:
                return None
        return self.session.token

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Persist current tokens to .env.client.local."""
        save_client_tokens(
            server_token=self.session.token if self.session else "",
            refresh_token=self._supabase_refresh_token,
            email=self.session.email if self.session else "",
        )

    def _restore_saved_session(self) -> None:
        """Restore a saved server token into ``self.session``.

        Returns:
            None.
        """
        claims = _read_jwt_claims(self._env.saved_server_token)
        self.session = ServerSession(
            token=self._env.saved_server_token,
            expires_at=int(claims.get("exp", 0)),
            email=self._env.saved_user_email,
            is_vip=claims.get("is_vip", False),
        )

    @staticmethod
    def _find_free_port() -> int:
        """Find a free TCP port on localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
