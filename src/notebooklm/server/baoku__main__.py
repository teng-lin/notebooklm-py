import argparse
import os
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="baoku-server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--dev", action="store_true", help="Enable CORS for localhost:5173")
    args = parser.parse_args()

    if args.dev:
        os.environ["BAOKU_DEV"] = "1"

    uvicorn.run(
        "notebooklm.server.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
