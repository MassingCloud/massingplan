"""Extension singletons, defined here so nothing has to import the app factory.

Sixteen lines that break every circular import in a Flask application. Models
import `db`, blueprints import `login_manager`, and neither imports
`create_app`.
"""

from __future__ import annotations

from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()
