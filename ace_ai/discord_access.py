"""Discord 單次請求權限；只讀 Discord 身分，不保存會員資格。"""
from dataclasses import dataclass
import re
import uuid

GENERAL_AI_ROLES = frozenset({"權證", "權證2", "已訂閱"})
WARRANT_AI_ROLES = frozenset({"權證", "權證2"})
UNLOCK_URL = "https://www.skool.com/ace-stock-3244/classroom"
GENERAL_DENIED = "此 AI 分析功能僅開放艾斯會員使用。"
WARRANT_DENIED = ("🔒 此功能尚未解鎖\n\n權證分點、事件勝率、分點部位與籌碼追蹤功能，"
                  "\n僅開放「權證／權證2」會員使用。\n\n購買艾斯DC權證系統即可解鎖此功能。")
ADMIN_DENIED = "此功能只限伺服器管理員或 SUPERUSER 使用，請使用真正的 /ace 指令。"


class AccessDenied(Exception):
    def __init__(self, message, required="GENERAL"):
        super().__init__(message)
        self.required = required


@dataclass(frozen=True)
class UserEntitlement:
    general: bool = False
    warrant: bool = False
    admin: bool = False
    beta: bool = False

    @classmethod
    def from_member(cls, member, superuser_ids=(), beta_ids=()):
        if member.id in superuser_ids:
            return cls(True, True, True, True)
        roles = {str(getattr(role, "name", "")).strip() for role in getattr(member, "roles", ())}
        permissions = getattr(member, "guild_permissions", None)
        admin = bool(getattr(permissions, "administrator", False) or getattr(permissions, "manage_guild", False))
        return cls(bool(roles & GENERAL_AI_ROLES), bool(roles & WARRANT_AI_ROLES), admin, member.id in beta_ids)


SIMULATED_ENTITLEMENTS = {
    "guest": UserEntitlement(),
    "general": UserEntitlement(True),
    "warrant": UserEntitlement(True, True),
    "beta": UserEntitlement(True, True, False, True),
    "superuser": UserEntitlement(True, True, True, True),
}


@dataclass(frozen=True)
class AccessContext:
    entitlement: UserEntitlement
    entry: str = "ask"
    simulation: str = ""
    nonce: str = ""

    @property
    def admin_mode(self):
        return self.entry == "ace" and self.entitlement.admin

    @property
    def partition(self):
        e = self.entitlement
        return f"{self.entry}:g{int(e.general)}w{int(e.warrant)}a{int(e.admin)}b{int(e.beta)}:{self.simulation}:{self.nonce}"

    def memory_key(self, key):
        # 正式 /ask 保留原使用者 key；/ace 與模擬內容不進正式追問記憶。
        if self.simulation:
            return f"simulation:{key}:{self.simulation}:{self.nonce}"
        return f"ace:{key}" if self.entry == "ace" else key


def resolve_access(member, superuser_ids, question, *, admin_entry=False, beta_ids=()):
    real = UserEntitlement.from_member(member, superuser_ids, beta_ids)
    if not admin_entry:
        return AccessContext(real), question
    if not real.admin:
        raise AccessDenied(ADMIN_DENIED, "ADMIN")
    match = re.match(r"^\s*測試(?:\s|$)", question)
    if match:
        parts = question.strip().split(maxsplit=2)
        if len(parts) != 3 or parts[1].lower() not in SIMULATED_ENTITLEMENTS:
            raise AccessDenied("用法：/ace 測試 guest|general|warrant|beta|superuser <問題>", "ADMIN")
        mode = parts[1].lower()
        return AccessContext(SIMULATED_ENTITLEMENTS[mode], "ace", mode, uuid.uuid4().hex), parts[2]
    return AccessContext(UserEntitlement(True, True, True, True), "ace"), question


@dataclass(frozen=True)
class FeaturePolicy:
    required: str = "GENERAL"
    beta_only: bool = False


FEATURE_POLICIES = {}  # 新功能可登記 route → FeaturePolicy；正常 /ace 全開。


def beta_allowed(access):
    """Beta 只看這次請求的 entitlement：SUPERUSER、BETA_TESTER_IDS、正常 /ace、/ace 測試 beta。"""
    return access is None or access.entitlement.beta


def beta_blocked(access, route):
    """route 標 beta_only 且這次請求不是 tester → True；呼叫端靜默改走舊 route／一般 fallback，不顯示任何 Beta 字樣。"""
    policy = FEATURE_POLICIES.get(route)
    return bool(policy and policy.beta_only and not beta_allowed(access))


def require_feature(access, policy=FeaturePolicy()):
    if access is None:  # 本機 CLI 沿用既有行為，Discord 入口一律傳入 AccessContext。
        return
    e = access.entitlement
    if policy.required == "ADMIN":
        if not access.admin_mode:
            raise AccessDenied(ADMIN_DENIED, "ADMIN")
    elif not e.general:
        raise AccessDenied(GENERAL_DENIED)
    elif policy.required == "WARRANT" and not e.warrant:
        raise AccessDenied(WARRANT_DENIED, "WARRANT")
    if policy.beta_only and not e.beta:
        raise AccessDenied("請換個說法，或指定股票名稱、代號與想查詢的項目。", "BETA")


def require_question(access, question, known_branches=(), parsed=None):
    require_feature(access)
    if access is None or access.entitlement.warrant:
        return
    value = re.sub(r"[\s_－-]+", "", question).upper()
    explicit = re.search(r"權證|分點|勝率|ABCDE|事件[A-E]|(?<![A-Z])[A-E](?:事件|類|級|型)|[A-E][～~至][A-E]", value)
    named = any(len(alias) >= 3 and re.sub(r"[\s_－-]+", "", alias).upper() in value
                for alias in known_branches)
    if explicit or named or (parsed and (parsed.branches or parsed.branch_candidates or parsed.event_type)):
        require_feature(access, FeaturePolicy("WARRANT"))
