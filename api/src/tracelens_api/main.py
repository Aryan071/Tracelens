"""`python -m tracelens_api.main`: serve the API with uvicorn."""

import os

import uvicorn

from tracelens_api import observability

HOST = "0.0.0.0"  # noqa: S104 - container service; the compose file publishes it on localhost only


def main() -> None:
    observability.setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(
        "tracelens_api.app:app",
        host=HOST,
        port=int(os.environ.get("API_PORT", "8080")),
        log_config=None,  # our JSON handler, not uvicorn's
    )


if __name__ == "__main__":
    main()
