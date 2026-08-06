"""The stdio entrypoint, and the only module that picks a transport.

Serving this over Streamable HTTP later means a second main of about this size calling `server.run(transport='streamable-http')`.
Nothing else moves — but read the credential warning in the README first: the session key is a whole personal account.
"""

import os
import sys

from .config import Settings, load_env_file
from .errors import ConfigError
from .server import build_server


def main() -> None:
	load_env_file()
	try:
		settings = Settings.from_env(os.environ)
	except ConfigError as exception:
		# stdout carries the MCP protocol, so operator-facing text goes to stderr.
		print(f"claude-projects-mcp: {exception}", file=sys.stderr)
		raise SystemExit(2) from exception

	build_server(settings).run(transport="stdio")


if __name__ == "__main__":
	main()
