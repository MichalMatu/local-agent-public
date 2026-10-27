"""Command-line interfaces for Local Agent."""


def register_portable_cli_parsers(subparsers) -> None:
    """Compose thin portable CLI adapters outside legacy diagnostics."""
    from local_agent.cli import host_remote, leases, overview, polling, publications, queue

    for register in (
        overview.register_overview_parser,
        leases.register_leases_parser,
        publications.register_publications_parser,
        queue.register_queue_parser,
        host_remote.register_host_remote_parser,
        polling.register_polling_parser,
        polling.register_menu_parser,
    ):
        register(subparsers)
