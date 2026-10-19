"""多店铺配置加载。

每家微信小店 = 一个 ShopConfig。多店共用一个回调 URL 时，用 URL 路径区分：
    /wxstore/{shop_slug}/webhook

店铺 slug 由本模块自动从 shop_user_name 派生（去掉 gh_ 前缀并小写）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ShopConfig:
    """单店铺配置。"""

    shop_user_name: str           # 微信小店 ToUserName（gh_xxx），多店路由关键字段
    app_id: str                   # 小店应用 AppID
    app_secret: str               # 小店应用 AppSecret
    token: str                    # 自研消息推送 Token
    encoding_aes_key: str         # 43 字符 EncodingAESKey
    weiyu_base_url: str           # 微语 bytedesk 服务地址
    weiyu_tenant_token: str       # 该店铺对应的微语租户鉴权 token
    weiyu_org_uid: str = "df_org_uid"      # 微语组织 UID
    weiyu_agent_uid: str = "df_ag_uid"     # 微语默认客服 UID（转人工目标）
    weiyu_agent_user_uid: str = ""         # 微语登录用户 UID（MQTT/订阅使用，通常不是 agent_uid）
    default_welcome: str = "您好，很高兴为您服务 ~"
    keywords_to_human: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        """URL 路径用的简短标识。"""
        if self.shop_user_name.startswith("gh_"):
            return self.shop_user_name[3:].lower()
        return self.shop_user_name.lower()


# 运行期单例：shop_user_name -> ShopConfig 和 slug -> ShopConfig
_SHOPS_BY_TO_USER: dict[str, ShopConfig] = {}
_SHOPS_BY_SLUG: dict[str, ShopConfig] = {}


def load_shops() -> dict[str, ShopConfig]:
    """从 SHOPS_JSON 环境变量加载所有店铺配置。重复调用会全量覆盖内存表。"""
    global _SHOPS_BY_TO_USER, _SHOPS_BY_SLUG
    raw = os.getenv("SHOPS_JSON", "[]").strip()
    if not raw:
        raise RuntimeError("SHOPS_JSON 环境变量未配置或为空")

    items = json.loads(raw)
    by_to_user: dict[str, ShopConfig] = {}
    by_slug: dict[str, ShopConfig] = {}
    for item in items:
        cfg = ShopConfig(
            shop_user_name=item["shop_user_name"],
            app_id=item["app_id"],
            app_secret=item["app_secret"],
            token=item["token"],
            encoding_aes_key=item["encoding_aes_key"],
            weiyu_base_url=item["weiyu_base_url"],
            weiyu_tenant_token=item["weiyu_tenant_token"],
            weiyu_org_uid=item.get("weiyu_org_uid", "df_org_uid"),
            weiyu_agent_uid=item.get("weiyu_agent_uid", "df_ag_uid"),
            weiyu_agent_user_uid=item.get("weiyu_agent_user_uid", ""),
            default_welcome=item.get("default_welcome", "您好，很高兴为您服务 ~"),
            keywords_to_human=item.get("keywords_to_human", []),
        )
        if cfg.shop_user_name in by_to_user:
            raise RuntimeError(f"重复的 shop_user_name: {cfg.shop_user_name}")
        if cfg.slug in by_slug:
            raise RuntimeError(f"重复的 slug: {cfg.slug}")
        by_to_user[cfg.shop_user_name] = cfg
        by_slug[cfg.slug] = cfg

    _SHOPS_BY_TO_USER = by_to_user
    _SHOPS_BY_SLUG = by_slug
    return by_to_user


def get_shop_by_to_user(to_user_name: str) -> Optional[ShopConfig]:
    """按 ToUserName 取店铺配置（POST 解密后路由用）。"""
    return _SHOPS_BY_TO_USER.get(to_user_name)


def get_shop_by_slug(slug: str) -> Optional[ShopConfig]:
    """按 URL slug 取店铺配置（GET 验签 / POST 入口路由用）。"""
    return _SHOPS_BY_SLUG.get(slug)


def all_shops() -> list[ShopConfig]:
    return list(_SHOPS_BY_TO_USER.values())


# ---- 微语 MySQL 直连配置（用于轮询客服回复）----
@dataclass
class WeiyuMysqlConfig:
    host: str = "127.0.0.1"
    port: int = 13306
    user: str = "root"
    password: str = ""
    database: str = "bytedesk"


def load_weiyu_mysql() -> WeiyuMysqlConfig:
    return WeiyuMysqlConfig(
        host=os.getenv("WEIYU_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("WEIYU_MYSQL_PORT", "13306")),
        user=os.getenv("WEIYU_MYSQL_USER", "root"),
        password=os.getenv("WEIYU_MYSQL_PASSWORD", ""),
        database=os.getenv("WEIYU_MYSQL_DATABASE", "bytedesk"),
    )
