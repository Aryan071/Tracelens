"""`python -m tracelens_api.main`: serve the API with uvicorn."""

import os

import uvicorn

HOST = "0.0.0.0"  # noqa: S104 - container service; the compose file publishes it on localhost only


def main() -> None:
    uvicorn.run("tracelens_api.app:app", host=HOST, port=int(os.environ.get("API_PORT", "8080")))


if __name__ == "__main__":
    main()
