"""Read-only data-validation harness for the XTS ingestion pipeline.

Run modules from the ``backend`` directory with the project venv, e.g.::

    .venv\\Scripts\\python.exe -m validation.layer_b_internal

HARD RULE: nothing in this package may call ``POST /auth/login`` — a fresh
XTS login invalidates the running feed's token (single session per appKey).
The harness reuses the token already persisted in ``auth_sessions``.
"""
