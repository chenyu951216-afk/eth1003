import pytest
from fastapi.testclient import TestClient
from eth1003 import web


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(web,'AUTH',tmp_path/'auth.json')
    monkeypatch.setattr(web,'DATA',tmp_path)
    monkeypatch.setattr(web,'_local',lambda request:True)
    web.runtime.update(mode='demo',store=None,bitget=None,runner=None,
                       armed=False,automatic=False,last_result=None,sessions={})
    with TestClient(web.app) as c:yield c


def logged_in(client):
    assert client.post('/api/setup',json={'password':'long-testing-pass-123'}).status_code==200
    result=client.post('/api/login',json={'password':'long-testing-pass-123'})
    assert result.status_code==200
    return {'x-csrf-token':result.json()['csrf']}


def test_site_home_and_first_run_setup(client):
    assert client.get('/').status_code==200
    assert client.get('/api/bootstrap').json()['needs_setup']
    assert client.get('/api/status').status_code==401
    headers=logged_in(client)
    assert client.get('/api/status').json()['armed'] is False
    assert client.get('/api/session').json()['csrf']==headers['x-csrf-token']


def test_all_trade_endpoints_are_closed_by_default(client):
    headers=logged_in(client)
    for endpoint in ('/api/run','/api/close','/api/cancel','/api/modify','/api/protection'):
        assert client.post(endpoint,json={},headers=headers).status_code in {409,422}


def test_csrf_blocks_connection_and_live_arming(client):
    headers=logged_in(client)
    body={'mode':'live','account_type':'classic','key':'k','secret':'s','passphrase':'p'}
    assert client.post('/api/connect',json=body).status_code==403
    assert client.post('/api/connect',json=body,headers=headers).status_code==200
    assert not web.runtime['armed']
    assert client.post('/api/arm',json={'phrase':'ENABLE DEMO TRADING'},headers=headers).status_code==400
    assert client.post('/api/arm',json={'phrase':'ENABLE LIVE TRADING'},headers=headers).status_code==200
    assert web.runtime['armed']
    assert client.post('/api/disarm',headers=headers).json()['armed'] is False


def test_daily_report_requires_login(client):
    assert client.get('/daily.csv').status_code==401
    logged_in(client)
    report=client.get('/daily.csv')
    assert report.status_code==200
    assert '當日交易盈虧' in report.text


def test_form_error_is_plain_chinese(client):
    headers=logged_in(client)
    response=client.post('/api/connect',json={'mode':'demo'},headers=headers)
    assert response.status_code==422
    assert '表單資料不完整' in response.json()['error']
    assert '交易所金鑰' in response.json()['error']
