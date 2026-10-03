import asyncio
import os
import stat

import pytest
from mcp.shared.auth import OAuthToken

from agent.mcp_auth import FileTokenStorage, LoginRequired, _wait_for_callback, build_oauth, is_allowed_domain


def test_phishing_guard_domains():
    allowed = ["robinhood.com"]
    assert is_allowed_domain("robinhood.com", allowed)
    assert is_allowed_domain("login.robinhood.com", allowed)
    assert not is_allowed_domain("robinhood.com.evil.io", allowed)
    assert not is_allowed_domain("evilrobinhood.com", allowed)


def test_tokens_are_stored_owner_only(tmp_path):
    storage = FileTokenStorage(tmp_path / "mcp_oauth.json")
    asyncio.run(storage.set_tokens(OAuthToken(access_token="secret", token_type="Bearer")))
    mode = stat.S_IMODE(os.stat(tmp_path / "mcp_oauth.json").st_mode)
    assert mode == 0o600
    assert asyncio.run(storage.get_tokens()).access_token == "secret" and storage.has_tokens()


def test_unattended_run_never_opens_a_browser(tmp_path):
    provider = build_oauth("https://agent.robinhood.com/mcp/trading", FileTokenStorage(tmp_path / "t.json"),
                           8765, interactive=False, allowed_auth_domains=["robinhood.com"])
    with pytest.raises(LoginRequired, match="--login"):
        asyncio.run(provider.context.redirect_handler("https://robinhood.com/oauth/authorize?x=1"))


def test_login_refuses_non_robinhood_page(tmp_path):
    provider = build_oauth("https://agent.robinhood.com/mcp/trading", FileTokenStorage(tmp_path / "t.json"),
                           8765, interactive=True, allowed_auth_domains=["robinhood.com"])
    with pytest.raises(LoginRequired, match="phishing"):
        asyncio.run(provider.context.redirect_handler("https://robinhood-login.example.com/authorize"))


def test_callback_server_returns_code_and_state():
    async def scenario():
        task = asyncio.create_task(_wait_for_callback(18765, timeout_s=5))
        await asyncio.sleep(0.2)
        reader, writer = await asyncio.open_connection("127.0.0.1", 18765)
        writer.write(b"GET /callback?code=abc&state=xyz HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        await reader.read()
        writer.close()
        return await task

    assert asyncio.run(scenario()) == ("abc", "xyz")
