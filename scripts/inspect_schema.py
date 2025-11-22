print("Script starting...")
import sys
import os

# Add project root to sys.path
sys.path.append(os.getcwd())

try:
    from sqlalchemy import inspect
    from app.core.database import engine
    print("Imports successful.")
except Exception as e:
    print(f"Import failed: {e}")
    sys.exit(1)

def inspect_users_table():
    print(f"Inspecting database...")
    try:
        with engine.connect() as _:
            print("Connection successful.")
            inspector = inspect(engine)

            # Check for table existence (handling potential case sensitivity or schema issues)
            table_names = inspector.get_table_names()
            print(f"Tables found: {table_names}")

            target_table = "users"
            if target_table not in table_names:
                print(f"Table '{target_table}' not found exactly. Checking case variations...")
                for t in table_names:
                    if t.lower() == target_table:
                        target_table = t
                        break
                else:
                    print(f"Table '{target_table}' definitely does not exist.")
                    return

            columns = inspector.get_columns(target_table)
            print(f"\nColumns in '{target_table}' table:")
            for column in columns:
                print(f"- {column['name']} ({column['type']})")
    except Exception as e:
        print(f"Error during inspection: {e}")

if __name__ == "__main__":
    inspect_users_table()
