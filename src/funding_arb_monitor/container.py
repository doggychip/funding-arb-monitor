from __future__ import annotations

import os


APP_UID = 10_001
APP_GID = 10_001


def drop_privileges() -> None:
    if os.getuid() != 0:
        return
    os.setgroups([])
    os.setgid(APP_GID)
    os.setuid(APP_UID)


def main() -> None:
    drop_privileges()
    os.execvp(
        "funding-arb-monitor",
        [
            "funding-arb-monitor",
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            os.getenv("PORT", "8080"),
        ],
    )


if __name__ == "__main__":
    main()
