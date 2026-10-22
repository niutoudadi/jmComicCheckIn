"""禁漫签到客户端"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jmcomic import JmOption, create_option_by_file

logger = logging.getLogger("jmcheckin.client")

# JM 服务器按北京时间（UTC+8）划分每日签到。
# GitHub Actions runner 的系统时区是 UTC，若直接用 dt.date.today()，
# 在北京时间 00:00-08:00 之间会拿到"昨天"的日号，导致误判为"今日已签到"。
try:
    JM_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # 缺 tzdata 时退化为固定 UTC+8（1991 年后等价）
    JM_TZ = dt.timezone(dt.timedelta(hours=8))


def mask_uid(value: Any) -> str:
    """通过uid位数进行掩藏"""
    s = str(value).strip()
    if not s:
        return s
    if len(s) <= 2:
        return "*" * len(s)
    if len(s) <= 6:
        return s[0] + "*" * (len(s) - 2) + s[-1]
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


class JMCheckinError(Exception):
    """签到业务错误"""


class JMCheckinClient:
    """封装 jmcomic 的 api client，提供登录与签到能力"""

    def __init__(self, option_file: Optional[str] = None):
        if option_file:
            self.option = create_option_by_file(option_file)
        else:
            # 默认配置，关闭 jmcomic 内部日志
            self.option = JmOption.construct({"log": False})
        # 使用移动端 api 实现
        self.client = self.option.build_jm_client(impl="api")
        self.uid: Optional[str] = None

    def login(self, username: str, password: str) -> Dict[str, Any]:
        resp = self.client.login(username, password)
        data: Dict[str, Any] = resp.res_data
        uid = data.get("uid")
        if uid is None:
            raise JMCheckinError("登录响应中缺少 uid，登录失败")
        self.uid = str(uid)
        logger.info("账号 %s 登录成功 (uid=%s)", username, mask_uid(self.uid))
        return data

    def get_daily(self) -> Dict[str, Any]:
        resp = self.client.req_api(f"/daily?user_id={self.uid}")
        return resp.res_data

    @staticmethod
    def is_signed_today(data: Dict[str, Any], tz: Optional[ZoneInfo] = None) -> bool:
        """判断今天是否已签到。

        record 为当月签到日历: [[{date: 日, signed: bool}, ...], ...]
        今日的日号必须按 JM 服务器时区（默认北京时间）计算，不能用 runner 本地时区。
        """
        today = dt.datetime.now(tz or JM_TZ).day
        if data.get("signed") is True:
            return True
        for group in data.get("record") or []:
            for item in group or []:
                if not isinstance(item, dict):
                    continue
                try:
                    day = int(item.get("date"))
                except (TypeError, ValueError):
                    continue
                if day == today:
                    return bool(item.get("signed"))
        return False

    def check_in(self) -> Dict[str, Any]:
        daily = self.get_daily()
        daily_id = daily.get("daily_id")

        if self.is_signed_today(daily):
            logger.info("今日已签到，无需重复签到")
            return {"status": "already", "detail": daily}

        if not daily_id:
            raise JMCheckinError("未获取到 daily_id，无法签到")

        resp = self.client.req_api(
            "/daily_chk",
            get=False,
            require_success=False,
            data={"user_id": self.uid, "daily_id": daily_id},
        )

        # 保留原始响应：jmcomic 遇到非约定结构（例如 data 返回数组）只会抛 TypeError，
        # 不打原文就无法判断服务器到底返回了什么，线上只能靠重跑反推。
        raw = (resp.text or "").strip()
        brief = raw[:300]

        def _already_hint() -> Optional[Dict[str, Any]]:
            """服务器提示"今日已签到"时按 already 处理，避免误报失败"""
            if any(key in raw for key in ("已签到", "已打卡", "已经签到", "重复签到")):
                logger.info("签到接口提示今日已签到: %s", brief)
                return {"status": "already", "detail": brief}
            return None

        try:
            data = resp.res_data if resp.is_success else None
        except Exception as exc:  # 响应体不是约定的加密字符串，无法解码
            hint = _already_hint()
            if hint is not None:
                return hint
            raise JMCheckinError(
                f"签到接口响应无法解析({type(exc).__name__}: {exc})，原文: {brief or '空响应'}"
            ) from exc

        if data is None:
            hint = _already_hint()
            if hint is not None:
                return hint
            raise JMCheckinError(f"签到接口返回异常: {brief or '空响应'}")

        if not isinstance(data, dict):
            raise JMCheckinError(
                f"签到接口返回结构异常({type(data).__name__})，原文: {brief or '空响应'}"
            )

        logger.info("签到成功: %s", data.get("msg", ""))
        return {"status": "ok", "msg": data.get("msg", ""), "detail": data}
