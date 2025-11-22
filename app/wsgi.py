"""
WSGI Adapter for PythonAnywhere deployment.
Converts FastAPI's ASGI application to a WSGI application using a2wsgi.
"""
from a2wsgi import ASGIMiddleware  # type: ignore
from app.main import app

# This 'application' object is what PythonAnywhere's WSGI server looks for
application = ASGIMiddleware(app)  # type: ignore
