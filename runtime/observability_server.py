"""Run the local health/readiness/metrics endpoint."""

import argparse
import time

from .observability import default_observability


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9090)
    args = parser.parse_args()
    server = default_observability.serve(args.host, args.port)
    print(f"observability listening on http://{args.host}:{server.server_port}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
