"""Couche d'accès Supabase partagée par tout l'écosystème Triskell."""

from .client import (
    SupabaseClient,
    SupabaseConfig,
    SupabaseAuthError,
    SupabaseNotConfigured,
    get_client,
    set_client,
    reset_client,
)

__all__ = [
    "SupabaseClient",
    "SupabaseConfig",
    "SupabaseAuthError",
    "SupabaseNotConfigured",
    "get_client",
    "set_client",
    "reset_client",
]
