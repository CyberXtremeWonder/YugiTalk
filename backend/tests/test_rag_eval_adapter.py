"""批量评测登录复用回归，不调用真实服务。"""

import httpx
import pytest

import rag_eval_adapter as adapter


def test_batch_reuses_login_with_independent_conversations(monkeypatch):
    calls = []

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith('/auth/login'):
            return httpx.Response(200, json={'csrf_token': 'fixture-csrf'},
                                  headers={'set-cookie': 'session=fixture; Path=/'})
        assert 'session=fixture' in request.headers.get('cookie', '')
        if path.endswith('/me/conversations'):
            assert request.headers['X-CSRF-Token'] == 'fixture-csrf'
            return httpx.Response(201, json={'id': f'conversation-{len(calls)}'})
        if path.endswith('/runs'):
            return httpx.Response(201, json={'id': f'run-{len(calls)}'})
        if path.endswith('/messages'):
            return httpx.Response(200, json={'items': [
                {'role': 'assistant', 'content': 'answer', 'sources': []}
            ]})
        return httpx.Response(200, json={'status': 'succeeded'})

    original = httpx.Client
    clients = []

    def client_factory(**kwargs):
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(adapter.httpx, 'Client', client_factory)
    monkeypatch.setattr(adapter, 'config', lambda name: 'http://fixture.test'
                        if name == 'RAG_TEST_API_URL' else 'fixture')
    with adapter.rag_session() as rag:
        for _ in range(50):
            assert rag('question') == {'answer': 'answer', 'sources': []}
    assert calls.count('/api/v1/auth/login') == 1
    assert calls.count('/api/v1/me/conversations') == 50
    assert clients[0].is_closed


def test_login_rate_limit_stops_before_tasks(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429)

    original = httpx.Client
    monkeypatch.setattr(adapter.httpx, 'Client', lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs
    ))
    monkeypatch.setattr(adapter, 'config', lambda name: 'http://fixture.test'
                        if name == 'RAG_TEST_API_URL' else 'fixture')
    with pytest.raises(RuntimeError, match='登录限流'):
        with adapter.rag_session():
            pytest.fail('限流不应进入评测')
    assert calls == ['/api/v1/auth/login']
