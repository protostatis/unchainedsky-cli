import asyncio
import json
import unittest
from unittest import mock

from unchained_cli.chrome import CDPError, ChromeClient


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(json.dumps(message))
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return await self.messages.get()


class FakeWebsocketsModule:
    def __init__(self, websocket):
        self.websocket = websocket

    def connect(self, ws_url, ping_timeout=None):
        self.ws_url = ws_url
        self.ping_timeout = ping_timeout
        return self.websocket


class ChromeClientTests(unittest.TestCase):
    def test_resolve_tab_auto_returns_first_page_tab(self):
        client = ChromeClient()
        with mock.patch.object(
            client,
            "list_tabs",
            return_value=[{"id": "tab-1"}, {"id": "tab-2"}],
        ):
            self.assertEqual(client.resolve_tab("auto"), "tab-1")

    def test_js_eval_frame_uses_isolated_world_context(self):
        client = ChromeClient()
        with mock.patch.object(
            client,
            "list_frames",
            return_value=[{"index": 0, "frameId": "frame-1", "url": "", "name": ""}],
        ), mock.patch.object(
            client,
            "send",
            side_effect=[
                {"executionContextId": 77},
                {"result": {"type": "number", "value": 5}},
            ],
        ) as send:
            result = client.js_eval_frame("tab-1", "0", "2 + 3")

        self.assertEqual(result, 5)
        self.assertEqual(send.call_args_list[0].args[1], "Page.createIsolatedWorld")
        self.assertEqual(send.call_args_list[1].args[1], "Runtime.evaluate")
        self.assertEqual(send.call_args_list[1].args[2]["contextId"], 77)

    def test_wait_ready_dom_strategy_polls_document_ready_state(self):
        websocket = FakeWebSocket([
            {"id": 1, "result": {"result": {"type": "string", "value": "complete"}}},
        ])
        client = ChromeClient()

        async def run_test():
            with mock.patch.object(
                client,
                "_require_websockets",
                return_value=FakeWebsocketsModule(websocket),
            ):
                return await client._async_wait_ready("ws://example", "dom", timeout=0.1)

        result = asyncio.run(run_test())

        self.assertEqual(result, "ready")
        self.assertEqual(websocket.sent[0]["method"], "Runtime.enable")
        self.assertEqual(websocket.sent[1]["method"], "Runtime.evaluate")

    def test_wait_ready_network_strategy_waits_for_request_completion(self):
        websocket = FakeWebSocket([
            {"method": "Network.requestWillBeSent", "params": {"requestId": "req-1"}},
            {"method": "Network.loadingFinished", "params": {"requestId": "req-1"}},
        ])
        client = ChromeClient()

        async def run_test():
            with mock.patch("unchained_cli.chrome._NETWORK_IDLE_WINDOW", 0.0), mock.patch.object(
                client,
                "_require_websockets",
                return_value=FakeWebsocketsModule(websocket),
            ):
                return await client._async_wait_ready("ws://example", "network", timeout=0.1)

        result = asyncio.run(run_test())

        self.assertEqual(result, "ready")
        self.assertEqual(websocket.sent[0]["method"], "Runtime.enable")
        self.assertEqual(websocket.sent[1]["method"], "Network.enable")

    def test_wait_ready_timeout_raises_cdp_error(self):
        client = ChromeClient()
        with mock.patch.object(client, "_ws_url_for", return_value="ws://example"), mock.patch.object(
            client,
            "_async_wait_ready",
            new=mock.AsyncMock(side_effect=asyncio.TimeoutError),
        ):
            with self.assertRaisesRegex(CDPError, "strategy=dom"):
                client.wait_ready("tab-1", "dom", timeout=0.1)


if __name__ == "__main__":
    unittest.main()
