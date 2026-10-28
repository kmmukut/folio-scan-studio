"""Folio's route handlers, by area (see app.py for the shared parts)."""
from backend.routes import accounts, admin, devices, documents, printing, tasks

ROUTERS = [accounts.router, devices.router, printing.router, documents.router, admin.router, tasks.router]
