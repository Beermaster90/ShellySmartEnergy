import re
from typing import Union


class SecurityUtils:
    """Utility class for handling security-sensitive operations like sanitizing logs."""

    # Patterns to match sensitive information
    SENSITIVE_PATTERNS = [
        # API keys and auth tokens (various formats)
        (r"auth_key=[A-Za-z0-9+/=]{20,}", "auth_key=***HIDDEN***"),
        (r"api_key=[A-Za-z0-9+/=]{20,}", "api_key=***HIDDEN***"),
        (r"token=[A-Za-z0-9+/=]{20,}", "token=***HIDDEN***"),
        (r"key=[A-Za-z0-9+/=]{20,}", "key=***HIDDEN***"),
        # ENTSOE API key patterns
        (
            r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}",
            "***ENTSOE-KEY-HIDDEN***",
        ),
        # Shelly auth keys (typically base64-like strings)
        (
            r"[A-Za-z0-9+/]{32,}={0,2}",
            lambda m: (
                f"***SHELLY-KEY-{m.group()[:4]}...{m.group()[-4:]}***"
                if len(m.group()) > 20
                else "***HIDDEN***"
            ),
        ),
        # General long alphanumeric strings that might be tokens
        (r"\b[A-Za-z0-9]{40,}\b", "***LONG-TOKEN-HIDDEN***"),
        # Password patterns in URLs
        (r"password=[^&\s]+", "password=***HIDDEN***"),
        (r"passwd=[^&\s]+", "passwd=***HIDDEN***"),
    ]

    @staticmethod
    def sanitize_message(message: Union[str, Exception]) -> str:
        """
        Sanitize error messages or any string by hiding sensitive information.

        Args:
            message: String or Exception containing potentially sensitive data

        Returns:
            Sanitized string with sensitive information replaced
        """
        if not message:
            return str(message)

        # Convert to string if it's an exception
        text = str(message)

        # Apply all sanitization patterns
        for pattern, replacement in SecurityUtils.SENSITIVE_PATTERNS:
            if callable(replacement):
                text = re.sub(pattern, replacement, text)
            else:
                text = re.sub(pattern, replacement, text)

        return text

    @staticmethod
    def sanitize_url(url: str) -> str:
        """
        Specifically sanitize URLs by hiding query parameters that contain sensitive data.

        Args:
            url: URL string that may contain sensitive parameters

        Returns:
            URL with sensitive parameters masked
        """
        if not url:
            return url

        # Split URL into base and query parts
        if "?" not in url:
            return url

        base_url, query_string = url.split("?", 1)

        # Sanitize the query string
        sanitized_query = SecurityUtils.sanitize_message(query_string)

        return f"{base_url}?{sanitized_query}"

    @staticmethod
    def get_safe_error_message(error: Exception, context: str = "") -> str:
        """
        Get a safe error message for logging or display.

        Args:
            error: Exception object
            context: Additional context for the error

        Returns:
            Safe error message with sensitive data hidden
        """
        error_msg = SecurityUtils.sanitize_message(str(error))

        if context:
            return f"{context}: {error_msg}"
        return error_msg


def validate_outbound_url(value):
    """Reject server-side-request-forgery targets on user-editable API URLs.

    shelly_server and tuya_base_url are edited by users and then fetched by the
    server, with the device's API key attached as a query parameter. Without
    this, a user can point them at cloud metadata (169.254.169.254), at other
    services on the tunnel host, or at a host they control to capture the key.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    from django.core.exceptions import ValidationError

    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValidationError("API server URL must use https://")
    if not parsed.hostname:
        raise ValidationError("API server URL must include a hostname")

    try:
        resolved = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise ValidationError(f"Cannot resolve host: {parsed.hostname}")

    for entry in resolved:
        address = ipaddress.ip_address(entry[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            raise ValidationError(
                f"API server URL must point at a public address, not {address}"
            )
