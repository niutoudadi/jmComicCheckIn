"""client.py 的单元测试"""
import datetime as dt

import pytest
from freezegun import freeze_time

from jm_checkin.client import JMCheckinClient, JMCheckinError, JM_TZ, mask_uid


def test_mask_uid():
    assert mask_uid('') == ''
    assert mask_uid('12') == '**'
    assert mask_uid('123456') == '1****6'
    assert mask_uid('12345678') == '12****78'


class _Resp:
    """模拟 jmcomic 的 JmApiResp 关键属性"""

    def __init__(self, data, success=True, text=''):
        self.res_data = data
        self.is_success = success
        self.text = text


class _FakeApi:
    """模拟 jmcomic 的 api 客户端"""

    def __init__(self, login_data=None, daily_data=None, chk_data=None, chk_success=True):
        self.login_data = login_data if login_data is not None else {'uid': '123456'}
        self.daily_data = daily_data if daily_data is not None else {'daily_id': '42'}
        self.chk_data = chk_data if chk_data is not None else {'msg': 'ok'}
        self.chk_success = chk_success
        self.login_calls = []
        self.req_calls = []

    def login(self, username, password):
        self.login_calls.append((username, password))
        return _Resp(self.login_data)

    def req_api(self, url, **kwargs):
        self.req_calls.append((url, kwargs))
        if url == '/daily_chk':
            return _Resp(self.chk_data, success=self.chk_success)
        if url.startswith('/daily'):
            return _Resp(self.daily_data)
        raise AssertionError(f'unexpected url: {url}')


def _make_client(api):
    # 跳过 __init__，直接注入假对象
    client = JMCheckinClient.__new__(JMCheckinClient)
    client.client = api
    client.uid = None
    return client


def test_login_success():
    api = _FakeApi()
    client = _make_client(api)
    data = client.login('alice', 'p@ss')
    assert data['uid'] == '123456'
    assert client.uid == '123456'
    assert api.login_calls == [('alice', 'p@ss')]


def test_login_missing_uid():
    api = _FakeApi(login_data={})
    client = _make_client(api)
    with pytest.raises(JMCheckinError, match='uid'):
        client.login('alice', 'p@ss')


def test_is_signed_today_signed_flag():
    assert JMCheckinClient.is_signed_today({'signed': True}) is True


def test_is_signed_today_record_signed():
    today = dt.datetime.now(JM_TZ).day
    data = {'record': [[{'date': str(today), 'signed': True}]]}
    assert JMCheckinClient.is_signed_today(data) is True


def test_is_signed_today_record_unsigned():
    today = dt.datetime.now(JM_TZ).day
    data = {'record': [[{'date': str(today), 'signed': False}]]}
    assert JMCheckinClient.is_signed_today(data) is False


def test_is_signed_today_empty():
    assert JMCheckinClient.is_signed_today({}) is False


def test_is_signed_today_uses_jm_timezone_not_local():
    """回归测试：北京时间 00:00-08:00（UTC 仍是前一天）不得把昨天当成今天。

    复现场景：2026-10-19 18:53 UTC == 北京 2026-10-20 02:53，
    此时 19 号已签、20 号未签，必须判定为「未签到」，否则当天会漏签。
    """
    record = {'record': [[
        {'date': f'{d:02d}', 'signed': (d == 19), 'bonus': False}
        for d in range(1, 21)
    ]]}

    with freeze_time('2026-10-19 18:53:06'):
        # 模拟 GitHub runner 的 UTC 本地时区
        assert dt.date.today().day == 19, '前提：UTC 本地日期仍是 19 号'
        assert JMCheckinClient.is_signed_today(record) is False, \
            '按北京时间今天是 20 号且未签，不能误判为已签'
        assert JMCheckinClient.is_signed_today(record, JM_TZ) is False

    with freeze_time('2026-10-20 03:17:31'):
        # 北京 10-20 11:17，20 号确实已签 -> 应判为已签
        signed = {'record': [[{'date': f'{d:02d}', 'signed': (d == 20)} for d in range(1, 21)]]}
        assert JMCheckinClient.is_signed_today(signed) is True


def test_check_in_already_signed():
    api = _FakeApi(daily_data={'daily_id': '42', 'signed': True})
    client = _make_client(api)
    client.uid = '123456'
    result = client.check_in()
    assert result['status'] == 'already'
    assert not any(url == '/daily_chk' for url, _ in api.req_calls)


def test_check_in_success():
    api = _FakeApi(daily_data={'daily_id': '42'})
    client = _make_client(api)
    client.uid = '123456'
    result = client.check_in()
    assert result['status'] == 'ok'
    url, kwargs = api.req_calls[-1]
    assert url == '/daily_chk'
    assert kwargs['data'] == {'user_id': '123456', 'daily_id': '42'}


def test_check_in_missing_daily_id():
    api = _FakeApi(daily_data={})
    client = _make_client(api)
    client.uid = '123456'
    with pytest.raises(JMCheckinError, match='daily_id'):
        client.check_in()


def test_check_in_api_failure():
    api = _FakeApi(daily_data={'daily_id': '42'}, chk_success=False, chk_data={'msg': '请求失败'})
    client = _make_client(api)
    client.uid = '123456'
    with pytest.raises(JMCheckinError, match='签到接口返回异常'):
        client.check_in()
