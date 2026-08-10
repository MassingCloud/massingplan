"""WSGI entry point. `gunicorn wsgi:app` or `flask --app wsgi:app run`."""

from massingplan.app import create_app

app = create_app()
