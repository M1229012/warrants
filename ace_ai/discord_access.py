"""Discord 單次請求權限；只讀 Discord 身分，不保存會員資格。"""
from dataclasses import dataclass
import re
import uuid

# 會員角色：「已訂閱」＝一般 AI＋現股分點籌碼；「權證」「權證2」「權證3」…（每季新增）＝一般 AI＋權證分點籌碼。
# 權證角色一律用 is_warrant_role() 判斷，不要再寫死固定的角色集合，否則新季角色會有部分功能不能用。
SUBSCRIBER_ROLE = "已訂閱"
_WARRANT_ROLE_RE = re.compile(r"權證\d*")
UNLOCK_URL = "https://www.skool.com/ace-stock-3244/classroom"
GENERAL_DENIED = "此 AI 分析功能僅開放艾斯會員使用。"
WARRANT_DENIED = ("🔒 此功能尚未解鎖\n\n權證分點、事件勝率、分點部位與籌碼追蹤功能，"
                  "\n僅開放權證會員使用。\n\n購買艾斯DC權證系統即可解鎖此功能。")
ADMIN_DENIED = "此功能只限伺服器管理員或 SUPERUSER 使用，請使用真正的 /ace 指令。"
SPOT_DENIED = "現股分點籌碼｜此功能目前沒有使用權限。"


def is_warrant_role(role_name) -> bool:
    """「權證」「權證2」「權證10」… 才算；「權證會員」「非權證會員」「權證 2」這類一律不算（完整比對，不做子字串）。"""
    return bool(_WARRANT_ROLE_RE.fullmatch(str(role_name or "").strip()))


def is_subscriber_role(role_name) -> bool:
    return str(role_name or "").strip() == SUBSCRIBER_ROLE


def is_general_role(role_name) -> bool:
    """一般 AI：已訂閱或任一權證角色（未來新增的權證N 自動涵蓋）。"""
    return is_subscriber_role(role_name) or is_warrant_role(role_name)


class AccessDenied(Exception):
    def __init__(self, message, required="GENERAL"):
        super().__init__(message)
        self.required = required


@dataclass(frozen=True)
class UserEntitlement:
    general: bool = False
    warrant: bool = False       # WARRANT_CHIP：權證分點籌碼
    admin: bool = False
    beta: bool = False
    spot: bool = False          # SPOT_CHIP：現股券商分點籌碼

    @classmethod
    def from_member(cls, member, superuser_ids=(), beta_ids=()):
        if member.id in superuser_ids:
            return cls(True, True, True, True, True)
        roles = [str(getattr(role, "name", "")).strip() for role in getattr(member, "roles", ())]
        permissions = getattr(member, "guild_permissions", None)
        admin = bool(getattr(permissions, "administrator", False) or getattr(permissions, "manage_guild", False))
        return cls(general=any(is_general_role(r) for r in roles), warrant=any(is_warrant_role(r) for r in roles),
                   admin=admin, beta=member.id in beta_ids, spot=any(is_subscriber_role(r) for r in roles))


SIMULATED_ENTITLEMENTS = {
    "guest": UserEntitlement(),
    "general": UserEntitlement(general=True, spot=True),                 # 已訂閱
    "warrant": UserEntitlement(general=True, warrant=True),              # 任一權證角色
    "both": UserEntitlement(general=True, warrant=True, spot=True),      # 已訂閱＋權證
    "beta": UserEntitlement(True, True, False, True, True),
    "superuser": UserEntitlement(True, True, True, True, True),
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
        return (f"{self.entry}:g{int(e.general)}w{int(e.warrant)}s{int(e.spot)}a{int(e.admin)}b{int(e.beta)}"
                f":{self.simulation}:{self.nonce}")

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
            raise AccessDenied("用法：/ace 測試 guest|general|warrant|both|beta|superuser <問題>", "ADMIN")
        mode = parts[1].lower()
        return AccessContext(SIMULATED_ENTITLEMENTS[mode], "ace", mode, uuid.uuid4().hex), parts[2]
    return AccessContext(UserEntitlement(True, True, True, True, True), "ace"), question


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
    elif policy.required == "SPOT" and not e.spot:
        raise AccessDenied(SPOT_DENIED, "SPOT")
    if policy.beta_only and not e.beta:
        raise AccessDenied("請換個說法，或指定股票名稱、代號與想查詢的項目。", "BETA")


# ============================================================
# 籌碼類型：現股分點（SPOT）／權證分點（WARRANT）／兩種一起（COMBINED）
# ============================================================

# 裸「勝率」不算權證（「2330型態勝率」「外資勝率」不是權證）；要有 A～E 事件、事件勝率、權證勝率，或權證分點名稱＋勝率。
_EXPLICIT_WARRANT_CHIP_RE = re.compile(r"權證|ABCDE|事件勝率|(?<![A-Z])[A-E](?:事件|類|級|型)|事件[A-E]|[A-E][～~至][A-E]")
_WIN_RATE_RE = re.compile(r"勝率")
_EXPLICIT_SPOT_CHIP_RE = re.compile(r"現股|券商分點|集中度")
_COMBINED_CHIP_RE = re.compile(r"(?:兩種|兩個|二種|雙).{0,6}(?:籌碼|分點)?.{0,4}(?:一起|比較|對照)|(?:一起|比較|對照).{0,4}(?:兩種|兩個)")
_AMBIGUOUS_CHIP_RE = re.compile(r"籌碼|分點|主力|誰.{0,2}在?買|誰.{0,2}買最多|吃貨|有沒有進|加碼|減碼|大戶|布局|佈局|在買什麼|買什麼|跑了沒|出貨|部位")
_INSTITUTIONAL_ONLY_RE = re.compile(r"外資|投信|自營商|三大法人|法人")


def chip_type(question, entitlement=None, known_branch=False, remembered="", warrant_branch=False):
    """回傳 "spot" / "warrant" / "combined" / ""（不是籌碼題）。

    明確字眼優先（現股＋權證或「兩種一起」＝combined；權證／A～E 事件／事件勝率＝warrant；現股／券商分點＝spot）；
    「勝率」只有搭配權證分點名稱（warrant_branch）才算權證；模糊的「籌碼、分點、主力、誰在買」：
    有 SPOT 權限就 spot，只有 WARRANT 才 warrant；追問沿用 remembered。
    外資／投信／三大法人是法人籌碼（institutional），不是券商分點。
    """
    value = re.sub(r"[\s_－-]+", "", str(question or "")).upper()
    warrant = bool(_EXPLICIT_WARRANT_CHIP_RE.search(value)) or (
        warrant_branch and bool(_WIN_RATE_RE.search(value)) and not _EXPLICIT_SPOT_CHIP_RE.search(value))
    spot = bool(_EXPLICIT_SPOT_CHIP_RE.search(value))
    if (warrant and spot) or _COMBINED_CHIP_RE.search(value):
        return "combined"
    if warrant:
        return "warrant"
    if spot:
        return "spot"
    ambiguous = bool(_AMBIGUOUS_CHIP_RE.search(value)) or known_branch
    if _INSTITUTIONAL_ONLY_RE.search(value) and not known_branch:
        return ""
    if not ambiguous:
        return ""   # 「那這檔上面有壓嗎」是技術面追問，不沿用籌碼類型
    if remembered in ("spot", "warrant", "combined"):
        return remembered
    e = entitlement
    if e is not None and not e.spot and e.warrant:
        return "warrant"
    return "spot"


def require_chip(access, kind):
    """籌碼權限在任何 Tool 執行前檢查；combined 只要有一種權限就放行（沒權限的那一半由呼叫端不執行並顯示鎖定卡）。"""
    if access is None or not kind:
        return
    require_feature(access)
    e = access.entitlement
    if kind == "spot":
        require_feature(access, FeaturePolicy("SPOT"))
    elif kind == "warrant":
        require_feature(access, FeaturePolicy("WARRANT"))
    elif kind == "combined" and not (e.spot or e.warrant):
        require_feature(access, FeaturePolicy("SPOT"))


def require_question(access, question, known_branches=(), parsed=None):
    require_feature(access)
    if access is None:
        return
    value = re.sub(r"[\s_－-]+", "", question).upper()
    named = any(len(alias) >= 3 and re.sub(r"[\s_－-]+", "", alias).upper() in value
                for alias in known_branches)
    remembered = getattr(parsed, "chip", "") if parsed is not None else ""
    warrant_named = named or bool(parsed and (parsed.branches or parsed.branch_candidates))
    kind = chip_type(question, access.entitlement, known_branch=warrant_named, remembered=remembered,
                     warrant_branch=warrant_named)
    if not kind and parsed is not None and getattr(parsed, "event_type", ""):
        kind = "warrant"
    require_chip(access, kind)
