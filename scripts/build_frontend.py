"""Build the Vue 3 frontend for production deployment."""

import subprocess
import sys
from pathlib import Path


def main() -> None:
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if not frontend_dir.is_dir():
        print("frontend/ directory not found — skipping frontend build.")
        sys.exit(0)

    print("=== Installing frontend dependencies ===")
    subprocess.run(["npm", "install"], cwd=str(frontend_dir), check=True)

    print("=== Building frontend ===")
    subprocess.run(["npm", "run", "build"], cwd=str(frontend_dir), check=True)

    dist = frontend_dir / "dist"
    if dist.is_dir():
        print(f"Frontend built successfully: {dist}")
    else:
        print("Frontend build failed: dist/ not found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
