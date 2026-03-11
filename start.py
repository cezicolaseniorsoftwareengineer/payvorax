"""
Quick start script for local development.
Bypasses dependency issues requiring compilation.
"""
import subprocess
import sys
import os
import hashlib

_REQ_FILE = "requirements-windows.txt"
_HASH_FILE = ".req-installed-hash"


def _needs_install() -> bool:
    """Returns True only when requirements-windows.txt changed since last install."""
    try:
        with open(_REQ_FILE, "rb") as f:
            current_hash = hashlib.md5(f.read()).hexdigest()
        if os.path.exists(_HASH_FILE):
            with open(_HASH_FILE) as f:
                if f.read().strip() == current_hash:
                    return False
        return True
    except OSError:
        return True


def _save_hash() -> None:
    try:
        with open(_REQ_FILE, "rb") as f:
            current_hash = hashlib.md5(f.read()).hexdigest()
        with open(_HASH_FILE, "w") as f:
            f.write(current_hash)
    except OSError:
        pass


def main():
    """Installs dependencies (only when changed) and starts the server."""
    print("Bio Code Tech Pay - Initialization\n")

    if _needs_install():
        print("Installing dependencies (requirements changed or first run)...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", _REQ_FILE, "-q"],
                check=True
            )
            _save_hash()
            print("Dependencies installed.\n")
        except subprocess.CalledProcessError:
            print("Error installing dependencies. Attempting to continue...\n")
    else:
        print("Dependencies up-to-date. Skipping install.\n")

    print("Starting FastAPI server on port 8000...")
    print("Frontend UI:  http://localhost:8000  <-- ACESSE AQUI")
    print("Documentation: http://localhost:8000/docs")
    print("Health check:  http://localhost:8000/health\n")

    os.environ["BIO_CODE_TECH_PAY_ALLOWED_START"] = "1"

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
