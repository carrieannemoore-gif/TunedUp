"""OAuth for the Robinhood Trading MCP server.

`--login` runs the browser flow once; tokens (and the dynamically registered client) are stored in
state/mcp_oauth.json with owner-only permissions and refreshed automatically afterwards. Unattended
runs never open a browser: if the tokens can no longer be refreshed, the agent stops and asks you
to log in again.
"""

from __future__ import annotations

import asyncio
import json
import os
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken


class LoginRequired(RuntimeError):
    pass


class FileTokenStorage(TokenStorage):
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)

    def has_tokens(self) -> bool:
        return bool(self._read().get("tokens"))

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


async def _wait_for_callback(port: int, timeout_s: float = 300) -> tuple[str, str | None]:
    """Serve one request on localhost:<port>/callback and return (code, state)."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[tuple[str, str | None]] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = (await reader.readline()).decode(errors="replace")
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        target = request_line.split(" ")[1] if " " in request_line else "/"
        query = parse_qs(urlparse(target).query)
        ok = "code" in query
        body = ("Robinhood login complete. You can close this tab." if ok
                else f"Login failed: {query.get('error', ['no code returned'])[0]}")
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                     f"Connection: close\r\n\r\n{body}".encode())
        await writer.drain()
        writer.close()
        if not result.done():
            if ok:
                result.set_result((query["code"][0], query.get("state", [None])[0]))
            else:
                result.set_exception(LoginRequired(body))

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        return await asyncio.wait_for(result, timeout_s)
    finally:
        server.close()


def is_allowed_domain(host: str, allowed: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in (a.lower() for a in allowed))


def build_oauth(server_url: str, storage: FileTokenStorage, port: int, interactive: bool,
                allowed_auth_domains: list[str]) -> OAuthClientProvider:
    metadata = OAuthClientMetadata(
        client_name="crypto-agent (personal)",
        redirect_uris=[f"http://localhost:{port}/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )

    async def redirect_handler(url: str) -> None:
        if not interactive:
            raise LoginRequired("Robinhood MCP login required. Run: python -m agent.main --login")
        host = urlparse(url).hostname or ""
        if not is_allowed_domain(host, allowed_auth_domains):
            raise LoginRequired(f"Refusing to open a login page on {host!r}: not in mcp.allowed_auth_domains "
                                f"{allowed_auth_domains}. This could be phishing; check the MCP URL.")
        print(f"\nOpening Robinhood login on {host}.")
        print(f"If the browser does not open, visit:\n{url}\n")
        webbrowser.open(url)

    async def callback_handler() -> tuple[str, str | None]:
        return await _wait_for_callback(port)

    return OAuthClientProvider(server_url, metadata, storage, redirect_handler, callback_handler)
