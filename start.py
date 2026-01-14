"""
Quick start script for local development.
Bypasses dependency issues requiring compilation.
"""
import subprocess
import sys


def main():
    """Installs dependencies and starts the server."""
    print("PayvoraX - Initialization\n")

    # Install simplified dependencies
    print("Installing dependencies...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", "requirements-windows.txt"],
            check=True
        )
        print("Dependencies installed successfully!\n")
    except subprocess.CalledProcessError:
        print("Error installing dependencies. Attempting to continue...\n")

    # Start server
    print("Starting FastAPI server on port 8000...")
    print("Frontend UI:  http://localhost:8000  <-- ACESSE AQUI")
    print("Documentation: http://localhost:8000/docs")
    print("Health check:  http://localhost:8000/health\n")

    # Set flag to allow application startup
    import os
    os.environ["PAYVORAX_ALLOWED_START"] = "1"

    # Force local SQLite database for development to avoid remote connection errors
    print("Configuring local database (SQLite)...")
    os.environ["DATABASE_URL"] = "sqlite:///./fintech.db"

    try:
        subprocess.run(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000"],
            check=True
        )
    except KeyboardInterrupt:
        print("\n\nServer stopped. Goodbye!")
    except Exception as e:
        print(f"\nError starting server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
