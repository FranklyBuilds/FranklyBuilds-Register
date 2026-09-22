"""国家代码 / 中文名 / 多源 IP 地理查询。"""

from __future__ import annotations

from typing import Any

# ISO 3166-1 alpha-2 → 中文常用名（覆盖注册/代理常见地区）
COUNTRY_ZH: dict[str, str] = {
    "AD": "安道尔", "AE": "阿联酋", "AF": "阿富汗", "AG": "安提瓜和巴布达",
    "AI": "安圭拉", "AL": "阿尔巴尼亚", "AM": "亚美尼亚", "AO": "安哥拉",
    "AR": "阿根廷", "AS": "美属萨摩亚", "AT": "奥地利", "AU": "澳大利亚",
    "AW": "阿鲁巴", "AZ": "阿塞拜疆", "BA": "波黑", "BB": "巴巴多斯",
    "BD": "孟加拉", "BE": "比利时", "BF": "布基纳法索", "BG": "保加利亚",
    "BH": "巴林", "BI": "布隆迪", "BJ": "贝宁", "BM": "百慕大",
    "BN": "文莱", "BO": "玻利维亚", "BR": "巴西", "BS": "巴哈马",
    "BT": "不丹", "BW": "博茨瓦纳", "BY": "白俄罗斯", "BZ": "伯利兹",
    "CA": "加拿大", "CD": "刚果(金)", "CF": "中非", "CG": "刚果(布)",
    "CH": "瑞士", "CI": "科特迪瓦", "CL": "智利", "CM": "喀麦隆",
    "CN": "中国", "CO": "哥伦比亚", "CR": "哥斯达黎加", "CU": "古巴",
    "CV": "佛得角", "CY": "塞浦路斯", "CZ": "捷克", "DE": "德国",
    "DJ": "吉布提", "DK": "丹麦", "DM": "多米尼克", "DO": "多米尼加",
    "DZ": "阿尔及利亚", "EC": "厄瓜多尔", "EE": "爱沙尼亚", "EG": "埃及",
    "ER": "厄立特里亚", "ES": "西班牙", "ET": "埃塞俄比亚", "FI": "芬兰",
    "FJ": "斐济", "FK": "福克兰群岛", "FR": "法国", "GA": "加蓬",
    "GB": "英国", "GD": "格林纳达", "GE": "格鲁吉亚", "GF": "法属圭亚那",
    "GG": "根西", "GH": "加纳", "GI": "直布罗陀", "GL": "格陵兰",
    "GM": "冈比亚", "GN": "几内亚", "GP": "瓜德罗普", "GQ": "赤道几内亚",
    "GR": "希腊", "GT": "危地马拉", "GU": "关岛", "GW": "几内亚比绍",
    "GY": "圭亚那", "HK": "中国香港", "HN": "洪都拉斯", "HR": "克罗地亚",
    "HT": "海地", "HU": "匈牙利", "ID": "印度尼西亚", "IE": "爱尔兰",
    "IL": "以色列", "IM": "马恩岛", "IN": "印度", "IQ": "伊拉克",
    "IR": "伊朗", "IS": "冰岛", "IT": "意大利", "JE": "泽西",
    "JM": "牙买加", "JO": "约旦", "JP": "日本", "KE": "肯尼亚",
    "KG": "吉尔吉斯斯坦", "KH": "柬埔寨", "KI": "基里巴斯", "KM": "科摩罗",
    "KN": "圣基茨和尼维斯", "KP": "朝鲜", "KR": "韩国", "KW": "科威特",
    "KY": "开曼群岛", "KZ": "哈萨克斯坦", "LA": "老挝", "LB": "黎巴嫩",
    "LC": "圣卢西亚", "LI": "列支敦士登", "LK": "斯里兰卡", "LR": "利比里亚",
    "LS": "莱索托", "LT": "立陶宛", "LU": "卢森堡", "LV": "拉脱维亚",
    "LY": "利比亚", "MA": "摩洛哥", "MC": "摩纳哥", "MD": "摩尔多瓦",
    "ME": "黑山", "MF": "法属圣马丁", "MG": "马达加斯加", "MH": "马绍尔群岛",
    "MK": "北马其顿", "ML": "马里", "MM": "缅甸", "MN": "蒙古",
    "MO": "中国澳门", "MP": "北马里亚纳", "MQ": "马提尼克", "MR": "毛里塔尼亚",
    "MS": "蒙特塞拉特", "MT": "马耳他", "MU": "毛里求斯", "MV": "马尔代夫",
    "MW": "马拉维", "MX": "墨西哥", "MY": "马来西亚", "MZ": "莫桑比克",
    "NA": "纳米比亚", "NC": "新喀里多尼亚", "NE": "尼日尔", "NG": "尼日利亚",
    "NI": "尼加拉瓜", "NL": "荷兰", "NO": "挪威", "NP": "尼泊尔",
    "NR": "瑙鲁", "NZ": "新西兰", "OM": "阿曼", "PA": "巴拿马",
    "PE": "秘鲁", "PF": "法属波利尼西亚", "PG": "巴布亚新几内亚", "PH": "菲律宾",
    "PK": "巴基斯坦", "PL": "波兰", "PR": "波多黎各", "PS": "巴勒斯坦",
    "PT": "葡萄牙", "PW": "帕劳", "PY": "巴拉圭", "QA": "卡塔尔",
    "RE": "留尼汪", "RO": "罗马尼亚", "RS": "塞尔维亚", "RU": "俄罗斯",
    "RW": "卢旺达", "SA": "沙特阿拉伯", "SB": "所罗门群岛", "SC": "塞舌尔",
    "SD": "苏丹", "SE": "瑞典", "SG": "新加坡", "SI": "斯洛文尼亚",
    "SK": "斯洛伐克", "SL": "塞拉利昂", "SM": "圣马力诺", "SN": "塞内加尔",
    "SO": "索马里", "SR": "苏里南", "SS": "南苏丹", "ST": "圣多美和普林西比",
    "SV": "萨尔瓦多", "SY": "叙利亚", "SZ": "斯威士兰", "TC": "特克斯和凯科斯",
    "TD": "乍得", "TG": "多哥", "TH": "泰国", "TJ": "塔吉克斯坦",
    "TL": "东帝汶", "TM": "土库曼斯坦", "TN": "突尼斯", "TO": "汤加",
    "TR": "土耳其", "TT": "特立尼达和多巴哥", "TV": "图瓦卢", "TW": "中国台湾",
    "TZ": "坦桑尼亚", "UA": "乌克兰", "UG": "乌干达", "US": "美国",
    "UY": "乌拉圭", "UZ": "乌兹别克斯坦", "VA": "梵蒂冈", "VC": "圣文森特",
    "VE": "委内瑞拉", "VG": "英属维尔京群岛", "VI": "美属维尔京群岛",
    "VN": "越南", "VU": "瓦努阿图", "WS": "萨摩亚", "YE": "也门",
    "YT": "马约特", "ZA": "南非", "ZM": "赞比亚", "ZW": "津巴布韦",
    "XK": "科索沃", "EU": "欧洲", "AP": "亚太",
}


def normalize_country_code(code: Any) -> str:
    c = str(code or "").strip().upper()
    if not c or c in ("??", "XX", "ZZ", "NONE", "NULL", "UNKNOWN", "N/A"):
        return ""
    if len(c) == 2 and c.isalpha():
        return c
    return ""


def country_zh(code: Any) -> str:
    c = normalize_country_code(code)
    if not c:
        return "未知"
    return COUNTRY_ZH.get(c, c)


def country_label(code: Any, name_zh: str | None = None) -> str:
    """展示用：日本 (JP) / 未知。"""
    c = normalize_country_code(code)
    if not c:
        zh = (name_zh or "").strip()
        return zh or "未知"
    zh = (name_zh or "").strip() or country_zh(c)
    if zh == c:
        return c
    return f"{zh} ({c})"


def lookup_ip_geo(proxy_url: str | None = None, timeout: float = 4.0) -> dict[str, Any]:
    """经代理（可选）查询出口 IP 国家。多源回退，减少 ??。"""
    import requests

    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

    # 各源解析为统一结构
    sources = (
        _from_ipinfo,
        _from_ipapi_co,
        _from_ip_api_com,
    )
    last_err = ""
    for fn in sources:
        try:
            info = fn(requests, proxies, timeout)
            code = normalize_country_code(info.get("country"))
            if code:
                info["country"] = code
                info["country_zh"] = info.get("country_zh") or country_zh(code)
                info["country_label"] = country_label(code, info.get("country_zh"))
                return info
            last_err = f"{fn.__name__}: empty country"
        except Exception as exc:
            last_err = f"{fn.__name__}: {exc}"
            continue

    return {
        "country": "",
        "country_zh": "未知",
        "country_label": "未知",
        "timezone": "UTC",
        "loc": None,
        "ip": "",
        "error": last_err or "geo lookup failed",
    }


def _from_ipinfo(requests, proxies, timeout) -> dict[str, Any]:
    r = requests.get(
        "https://ipinfo.io/json",
        proxies=proxies,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    d = r.json() if r.content else {}
    code = normalize_country_code(d.get("country"))
    return {
        "country": code,
        "timezone": d.get("timezone") or "UTC",
        "loc": d.get("loc"),
        "ip": d.get("ip") or "",
        "org": d.get("org") or "",
        "city": d.get("city") or "",
        "region": d.get("region") or "",
        "source": "ipinfo",
    }


def _from_ipapi_co(requests, proxies, timeout) -> dict[str, Any]:
    r = requests.get(
        "https://ipapi.co/json/",
        proxies=proxies,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    d = r.json() if r.content else {}
    if d.get("error"):
        raise RuntimeError(str(d.get("reason") or d.get("error")))
    code = normalize_country_code(d.get("country_code") or d.get("country"))
    return {
        "country": code,
        "country_zh": (d.get("country_name") and None) or country_zh(code),
        "timezone": d.get("timezone") or "UTC",
        "loc": (
            f"{d.get('latitude')},{d.get('longitude')}"
            if d.get("latitude") is not None and d.get("longitude") is not None
            else None
        ),
        "ip": d.get("ip") or "",
        "city": d.get("city") or "",
        "region": d.get("region") or "",
        "source": "ipapi.co",
    }


def _from_ip_api_com(requests, proxies, timeout) -> dict[str, Any]:
    # 免费接口：http://ip-api.com/json/  （经代理时看出口）
    r = requests.get(
        "http://ip-api.com/json/?fields=status,message,country,countryCode,city,regionName,lat,lon,timezone,query",
        proxies=proxies,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    d = r.json() if r.content else {}
    if str(d.get("status") or "").lower() != "success":
        raise RuntimeError(str(d.get("message") or "ip-api failed"))
    code = normalize_country_code(d.get("countryCode"))
    return {
        "country": code,
        "country_zh": country_zh(code),
        "timezone": d.get("timezone") or "UTC",
        "loc": (
            f"{d.get('lat')},{d.get('lon')}"
            if d.get("lat") is not None and d.get("lon") is not None
            else None
        ),
        "ip": d.get("query") or "",
        "city": d.get("city") or "",
        "region": d.get("regionName") or "",
        "source": "ip-api.com",
    }


def enrich_account_geo_fields(acc: dict) -> dict:
    """给账号字典补 country_zh / country_label，不写盘。"""
    if not isinstance(acc, dict):
        return acc
    code = normalize_country_code(acc.get("country"))
    zh = (acc.get("country_zh") or "").strip() or (country_zh(code) if code else "未知")
    acc["country"] = code or acc.get("country") or ""
    if not normalize_country_code(acc.get("country")):
        acc["country"] = ""
    acc["country_zh"] = zh if code else "未知"
    acc["country_label"] = country_label(code, acc["country_zh"])
    return acc
