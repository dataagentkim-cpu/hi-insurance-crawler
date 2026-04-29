"""
현대해상 가격공시실 보험료 수집기 (리팩토링 v2)
https://www.hi.co.kr/serviceAction.do

[사용법]
1. https://www.hi.co.kr/serviceAction.do 접속
2. F12 → Network 탭 → 상품 선택 후 보험료 산출 클릭
3. Console에 인터셉터 코드(--show-capture-guide) 붙여넣고 → 보험료 산출 클릭
4. 다운로드된 JSON을 해당 상품 파일명으로 저장
5. python3 hi_collect.py                  # 전체 수집
   python3 hi_collect.py --test           # 1회 테스트
   python3 hi_collect.py --skip-test      # 스킵그룹만 1조합 테스트
   python3 hi_collect.py --show-capture-guide
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    from curl_cffi import requests as _requests  # 보안설비 우회용
    _HAS_CURL_CFFI = True
except ImportError:
    import requests as _requests  # type: ignore
    _HAS_CURL_CFFI = False

import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────────────────────
TODAY    = datetime.today().strftime("%Y%m%d")
BASE     = "https://www.hi.co.kr"
API_URL  = f"{BASE}/ajax.xhi"
TRAN_ID  = "HHCA0030M07S"          # 보험료 산출 tranId
SLEEP_OK = 0.5                     # 정상 호출 후 대기
SLEEP_NET_RETRY = 3                # 네트워크 오류 재시도 대기

ROOT = Path(__file__).parent

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/serviceAction.do",
    "Origin":  BASE,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

PAYD_PERIODS_연만기 = ["Y010Y010", "Y015Y015", "Y020Y020", "Y030Y030"]
PAYD_PERIODS_세만기_1종 = [
    "Y010A090", "Y010A100", "Y015A090", "Y015A100",
    "Y020A090", "Y020A100", "Y025A090", "Y025A100", "Y030A100",
]
PAYD_PERIODS_세만기_2종 = [
    "Y020A090", "Y020A100", "Y025A090", "Y025A100", "Y030A100",
]

PAYLOAD_FILES: dict[str, tuple[str, list[str]]] = {
    "hi_payload_연만기_1종.json": ("연만기갱신형 1종(표준형)",            PAYD_PERIODS_연만기),
    "hi_payload_연만기_2종.json": ("연만기갱신형 2종(해약환급금미지급형)",  PAYD_PERIODS_연만기),
    "hi_payload_세만기_1종.json": ("세만기형 1종(표준형)",                PAYD_PERIODS_세만기_1종),
    "hi_payload_세만기_2종.json": ("세만기형 2종(해약환급금미지급형)",      PAYD_PERIODS_세만기_2종),
}

PERIOD_LABEL = {
    "Y005Y005": "5년",  "Y010Y010": "10년", "Y015Y015": "15년",
    "Y020Y020": "20년", "Y025Y025": "25년", "Y030Y030": "30년",
    "Y010A090": "10년납_90세만기",  "Y010A100": "10년납_100세만기",
    "Y015A090": "15년납_90세만기",  "Y015A100": "15년납_100세만기",
    "Y020A090": "20년납_90세만기",  "Y020A100": "20년납_100세만기",
    "Y025A090": "25년납_90세만기",  "Y025A100": "25년납_100세만기",
    "Y030A100": "30년납_100세만기",
}

BIRTH = {
    30: "19960701", 35: "19910701", 40: "19860701",
    45: "19810701", 50: "19760701", 55: "19710701",
    60: "19660701", 65: "19610701", 70: "19560701",
}
ALL_AGES = [30, 35, 40, 45, 50, 55, 60, 65, 70]
ALL_SEXES = [("1", "남"), ("2", "여")]

CODE_RE = r"[1-9][A-Z0-9]{3}"  # 담보 코드 정규식 (4자, 숫자/대문자)


# ─────────────────────────────────────────────────────────────────────────────
# 예외
# ─────────────────────────────────────────────────────────────────────────────
class HiApiError(RuntimeError):
    """API 비즈니스 예외 (responseStatus.exceptionOccurred = true)."""
    def __init__(self, code: str, message: str):
        super().__init__(f"{code} {message}")
        self.code = code or ""
        self.message = message or ""

    @property
    def text(self) -> str:
        return f"{self.code} {self.message}"


# ─────────────────────────────────────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────────────────────────────────────
log = logging.getLogger("hi_collect")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 페이로드 로드/패치
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Product:
    label: str
    periods: list[str]
    payload: dict


def load_payloads() -> list[Product]:
    found, missing = [], []
    for fname, (label, periods) in PAYLOAD_FILES.items():
        f = ROOT / fname
        if f.exists():
            found.append(Product(label, periods, json.loads(f.read_text(encoding="utf-8"))))
        else:
            missing.append(fname)

    if missing:
        log.warning("아직 캡처 안 된 파일: %s", ", ".join(missing))
    if not found:
        log.error("payload 파일이 하나도 없습니다. --show-capture-guide 로 가이드를 확인하세요.")
        sys.exit(1)
    return found


def load_coverage_amounts(prod_cd: str) -> dict[str, int]:
    """담보별 유효금액. extras/skip 있는 항목은 충돌 회피 위해 제외."""
    fname = "coverage_amounts_세만기.json" if prod_cd == "169D" else "coverage_amounts.json"
    f = ROOT / fname
    if not f.exists():
        return {}
    data = json.loads(f.read_text(encoding="utf-8"))
    return {
        r["cd"]: r["amt"]
        for r in data
        if r.get("amt") and not r.get("skip") and not r.get("extras")
    }


# --- make_payload 헬퍼 (단일 책임) ---
def _patch_pty(obj: Any, birth: str, sex_cd: str, reg_no: str) -> None:
    """피보험자 정보 일괄 패치."""
    items = obj if isinstance(obj, list) else [obj] if isinstance(obj, dict) else []
    for it in items:
        if not isinstance(it, dict):
            continue
        if "ptyBrdt"   in it: it["ptyBrdt"]   = birth
        if "ptySxdsCd" in it: it["ptySxdsCd"] = sex_cd
        if "ptyRegNo"  in it: it["ptyRegNo"]  = reg_no


def _get_elag_inner(req: dict) -> list[dict]:
    outer = req.get("elagInfoList", [])
    if not outer or not isinstance(outer[0], dict):
        return []
    return outer[0].get("elagInfoList", [])


def _patch_semaki_periods(elag_inner: list[dict], payd_period: str) -> None:
    """세만기형 담보별 paydInsdPeriod 갱신."""
    pay_yr = payd_period.split("A")[0]    # "Y020A100" → "Y020"
    for e in elag_inner:
        ep = e.get("paydInsdPeriod", "")
        if not ep:
            continue
        if ep.startswith("Y") and "A" in ep:
            e["paydInsdPeriod"] = payd_period
        elif ep.startswith("Z") and "Y" in ep:
            e["paydInsdPeriod"] = "Z999" + pay_yr


def _apply_extra_amounts(
    elag_inner: list[dict], extra: dict[str, int], payd_period: str | None
) -> None:
    is_semaki = bool(payd_period) and "A" in str(payd_period)
    pay_yr = payd_period.split("A")[0] if is_semaki else ""
    for e in elag_inner:
        cd = e.get("elagClsCd")
        if cd not in extra:
            continue
        e["elagWonInsdAmt"] = extra[cd]
        if is_semaki and not e.get("paydInsdPeriod"):
            nm = e.get("elagElpaNm", "")
            if any(k in nm for k in ("납입지원", "납입면제", "납입보장")):
                e["paydInsdPeriod"] = "Z999" + pay_yr
            else:
                e["paydInsdPeriod"] = payd_period


def make_payload(
    base: dict, birth: str, sex_cd: str,
    payd_period: str | None = None,
    extra_amounts: dict[str, int] | None = None,
) -> dict:
    """나이·성별·납입기간을 교체한 payload 반환."""
    p = copy.deepcopy(base)
    reg_no = birth[2:] + sex_cd + "000000"
    req = p.get("request", p)

    for key in ("insurdInfo2List", "insurdInfoList", "insurdList"):
        _patch_pty(req.get(key, []), birth, sex_cd, reg_no)

    lvo = req.get("ltapcommonVO", {})
    for fld in ("ctrtrPtyRegNo", "insrdPtyRegNo"):
        if fld in lvo:
            lvo[fld] = reg_no
    for fld in ("inagInsStDt", "savePremStDt", "premCalDt"):
        if fld in lvo:
            lvo[fld] = TODAY
    if payd_period and "paydInsdPeriod" in lvo:
        lvo["paydInsdPeriod"] = payd_period

    elag_inner = _get_elag_inner(req)
    if payd_period and "A" in str(payd_period):
        _patch_semaki_periods(elag_inner, payd_period)
    if extra_amounts:
        _apply_extra_amounts(elag_inner, extra_amounts, payd_period)

    return p


# ─────────────────────────────────────────────────────────────────────────────
# 세션 / API
# ─────────────────────────────────────────────────────────────────────────────
def init_session(retries: int = 3):
    s = (_requests.Session(impersonate="chrome120")
         if _HAS_CURL_CFFI else _requests.Session())
    s.headers.update({"User-Agent": HEADERS["User-Agent"]})
    last_exc: Exception | None = None
    for i in range(retries):
        try:
            s.get(f"{BASE}/serviceAction.do", timeout=15)
            return s
        except Exception as e:        # noqa: BLE001
            last_exc = e
            if i < retries - 1:
                time.sleep(SLEEP_NET_RETRY)
    assert last_exc is not None
    raise last_exc


def call_api(session, payload: dict) -> dict:
    r = session.post(API_URL, json=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def call_api_with_retry(session, payload: dict, retries: int = 3) -> dict:
    """네트워크 오류만 재시도. 비즈니스 에러는 즉시 raise."""
    last_exc: Exception | None = None
    for i in range(retries):
        try:
            return call_api(session, payload)
        except Exception as e:        # noqa: BLE001
            last_exc = e
            if i < retries - 1:
                time.sleep(SLEEP_NET_RETRY)
    assert last_exc is not None
    raise last_exc


def extract_elag_list(data: dict, only_priced: bool = True) -> list[dict]:
    outer = data.get("elagInfoList", [])
    if not outer or not isinstance(outer[0], dict):
        return []
    inner = outer[0].get("elagInfoList", [])
    if not only_priced:
        return inner
    return [e for e in inner if float(e.get("tpprPrem", 0) or 0) > 0]


def parse_response(resp: dict, age: int, sex_nm: str, period_label: str) -> list[dict]:
    rs = resp.get("responseStatus", {})
    if rs.get("exceptionOccurred"):
        exc = rs.get("exception", {})
        raise HiApiError(exc.get("code") or "", exc.get("message") or "")

    data   = resp.get("data", resp)
    common = data.get("ltapcommonVO", {})
    elag   = extract_elag_list(data)
    total  = float(common.get("insrdPrem", 0) or 0)
    basep  = float(common.get("premBasPrem", 0) or 0)

    return [
        {
            "나이":        age,
            "성별":        sex_nm,
            "납입기간":     period_label,
            "담보코드":     e.get("elagClsCd", ""),
            "담보명":       e.get("elagElpaNm", e.get("elagClsCd", "")),
            "담보보험료":    float(e.get("tpprPrem", 0) or 0),
            "기본보험료":    basep,
            "총납입보험료":   total,
        }
        for e in elag
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 에러 자동복구 헬퍼 (collect_one / collect_skipped_groups 공통)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class RecoveryResult:
    removed: int = 0
    fatal: bool = False
    message: str = ""


def recover_from_api_error(
    err: HiApiError,
    effective: dict[str, int],
    target_cds: set[str] | None = None,
    payload_amounts: dict[str, int] | None = None,
) -> RecoveryResult:
    """
    API 에러 메시지를 분석해 effective dict를 in-place 수정.
    target_cds: 그룹 수집 시 '제거 금지' 코드 (수집 대상 담보)
    payload_amounts: 세트가입 동반 코드 추가 시 사용할 기준 금액
    """
    msg = err.text
    target_cds = target_cds or set()
    res = RecoveryResult()

    # 1. 동시가입불가 (A코드 + B코드 → B 제거)
    if "동시가입불가" in msg:
        for m in re.finditer(rf"({CODE_RE})({CODE_RE})\[동시가입불가\]", msg):
            cd_remove = m.group(2)
            if cd_remove in effective and cd_remove not in target_cds:
                del effective[cd_remove]
                res.removed += 1
        if res.removed:
            return res

    # 2. 최대 가입금액 초과 (ULT01009보다 먼저 체크 — 같은 에러코드지만 처리 방식이 다름)
    if "최대" in msg and "천원이하" in msg:
        m_amt = re.search(r"최대\s*(\d+)\s*천원이하", msg)
        if m_amt:
            max_amt = int(m_amt.group(1))
            m_cd = re.search(rf"(?:167D|169D)({CODE_RE})", msg)
            if m_cd:
                cd_fix = m_cd.group(1)
                if effective.get(cd_fix) != max_amt:
                    effective[cd_fix] = max_amt
                    res.removed += 1
            else:
                for cd_t in target_cds:
                    if effective.get(cd_t, 0) > max_amt:
                        effective[cd_t] = max_amt
                        res.removed += 1
        if res.removed:
            return res

    # 3. ULT01009 / ULT00016 (연령·키구성 제한 → 해당 담보 삭제)
    if "ULT01009" in msg or ("ULT00016" in msg and "키구성" in msg):
        m = re.search(rf"담보:({CODE_RE})", msg)
        candidates = [m.group(1)] if m else re.findall(CODE_RE, msg)
        for cd in candidates:
            if cd in effective and cd not in target_cds:
                del effective[cd]
                res.removed += 1
                break
        if res.removed:
            return res

    # 4. 세트가입 / 필수가입 (동반 코드 추가)
    if ("세트가입" in msg or "필수가입" in msg) and payload_amounts is not None:
        m = re.search(r"피보험자\s+([A-Z0-9]{4,})\[(?:세트가|필수가)", msg)
        if m:
            req_str = m.group(1)
            req_codes = [req_str[i:i+4] for i in range(0, len(req_str), 4)
                         if len(req_str[i:i+4]) == 4]
            for rc in req_codes:
                if rc not in effective:
                    effective[rc] = payload_amounts.get(rc, 1000)
                    res.removed += 1
        if res.removed:
            return res

    # 5. 회복불가 케이스
    if "납만기를 선택" in msg:
        res.fatal = True
        res.message = "납만기 전용 담보"
        return res

    return res


# ─────────────────────────────────────────────────────────────────────────────
# 정상 수집
# ─────────────────────────────────────────────────────────────────────────────
def collect_one(
    session, prod: Product,
    ages: list[int], sexes: list[tuple[str, str]],
    extra_amounts: dict[str, int] | None = None,
    *, max_recover_attempts: int = 6,
) -> list[dict]:
    total = len(prod.periods) * len(ages) * len(sexes)
    rows: list[dict] = []
    n = 0
    log.info("▶ [%s]  %d종 × %d나이 × %d성별 = %d회",
             prod.label, len(prod.periods), len(ages), len(sexes), total)
    if extra_amounts:
        log.info("   담보 %d개 금액 설정됨", len(extra_amounts))

    for period in prod.periods:
        period_nm = PERIOD_LABEL.get(period, period)
        for age in ages:
            for sex_cd, sex_nm in sexes:
                n += 1
                effective = dict(extra_amounts or {})
                tag = f"[{n:>3}/{total}] {period_nm} {age}세 {sex_nm}"
                for attempt in range(max_recover_attempts):
                    payload = make_payload(prod.payload, BIRTH[age], sex_cd, period, effective)
                    try:
                        resp = call_api_with_retry(session, payload)
                        rows_one = parse_response(resp, age, sex_nm, period_nm)
                    except HiApiError as e:
                        if attempt < max_recover_attempts - 1:
                            r = recover_from_api_error(e, effective)
                            if r.fatal:
                                log.warning("%s: %s → 스킵", tag, r.message or e.text[:80])
                                break
                            if r.removed:
                                log.debug("%s: 자동복구 %d개 후 재시도", tag, r.removed)
                                continue
                        log.warning("%s: 오류 → %s", tag, e.text[:120])
                        break
                    except Exception as e:        # noqa: BLE001
                        log.error("%s: 실패 %s", tag, e)
                        break

                    if not rows_one:
                        log.info("%s: 담보 없음 (미지원 납입기간?)", tag)
                        break
                    for it in rows_one:
                        it["상품"] = prod.label
                    rows.extend(rows_one)
                    log.info("%s  총납입:%s원  담보:%d개",
                             tag, f"{int(rows_one[0]['총납입보험료']):,}", len(rows_one))
                    break
                time.sleep(SLEEP_OK)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 갱신형 보완 (세만기 ← 연만기)
# ─────────────────────────────────────────────────────────────────────────────
YEON_TYPE = {
    "연만기갱신형 1종(표준형)":           "1종",
    "연만기갱신형 2종(해약환급금미지급형)": "2종",
}
SEMAKI_TYPE = {
    "세만기형 1종(표준형)":            "1종",
    "세만기형 2종(해약환급금미지급형)":  "2종",
}


def _is_renewal_skip(rec: dict) -> bool:
    nm = rec.get("nm", "")
    skip = rec.get("skip", "")
    if not skip:
        return False
    if "인자값 오류" in skip:
        return True
    if skip == "max attempts":
        return "갱신형" in nm or (len(nm) == 40 and (nm.endswith("(갱") or nm.endswith("(갱신")))
    return False


def supplement_renewal_coverages(all_rows: list[dict]) -> list[dict]:
    """세만기 갱신형 담보를 연만기 결과에서 보완."""
    semaki_path = ROOT / "coverage_amounts_세만기.json"
    if not semaki_path.exists():
        return all_rows
    semaki_data = json.loads(semaki_path.read_text(encoding="utf-8"))
    renewal_cds = {r["cd"] for r in semaki_data if _is_renewal_skip(r)}
    if not renewal_cds:
        return all_rows

    yeon_idx: dict[tuple, dict] = {}
    for row in all_rows:
        t = YEON_TYPE.get(row.get("상품"))
        if t and row.get("담보코드") in renewal_cds:
            yeon_idx[(t, row["나이"], row["성별"], row["납입기간"], row["담보코드"])] = row
    if not yeon_idx:
        return all_rows

    yeon_periods = sorted({r["납입기간"] for r in all_rows if r.get("상품") in YEON_TYPE})
    semaki_combos = {(r["상품"], r["나이"], r["성별"]) for r in all_rows if r.get("상품") in SEMAKI_TYPE}

    new_rows: list[dict] = []
    for prod, age, sex in sorted(semaki_combos):
        t = SEMAKI_TYPE[prod]
        for period in yeon_periods:
            for cd in sorted(renewal_cds):
                src = yeon_idx.get((t, age, sex, period, cd))
                if src:
                    new_rows.append({
                        "상품": prod, "나이": age, "성별": sex,
                        "납입기간": period, "담보코드": cd, "담보명": src["담보명"],
                        "담보보험료": src["담보보험료"], "기본보험료": src["기본보험료"],
                        "총납입보험료": src["총납입보험료"],
                    })
    if new_rows:
        log.info("▶ 세만기 갱신형 담보 보완: %d행 추가 (납입기간 %d × 담보 %d)",
                 len(new_rows), len(yeon_periods), len(renewal_cds))
    return all_rows + new_rows


# ─────────────────────────────────────────────────────────────────────────────
# 스킵 그룹 수집 (생략된 부분은 원본과 동일 로직, 자동복구는 공통 헬퍼 사용)
# ─────────────────────────────────────────────────────────────────────────────
배상책임_그룹 = [
    {"all_codes": ["1ZRB", "1ZRC", "1ZRD"], "label": "배상책임(누수포함)"},
    {"all_codes": ["1ZRE", "1ZRF"],          "label": "배상책임(누수제외)"},
]
추가_그룹_167D: list[dict] = [
    {"all_codes": ["3LX3","3LX4","3LX5","3LX6","3LX7","3LX8","3LX9","3LY0","3LY1","3LY2"],
     "label": "남성통합암진단세트(연만기)", "only_sex": "1"},
    *[
        {"all_codes": [cd], "label": "동시가입불가_단독",
         "zero_cds": ["3SN6","3SR8","3LP2","3ND2","3ND3","3LP3"]}
        for cd in ["3LP2", "3ND2", "3ND3", "3LP3"]
    ],
]
추가_그룹_169D: list[dict] = [
    {"all_codes": ["3NM4","3NM5","3NM6","3NM7","3NM8","3NM9",
                   "3NN0","3NN1","3NN2","3NN3","3NN4","3NN5"],
     "label": "여성통합암진단세트", "only_sex": "2"},
    {"all_codes": ["3NL4","3NL5","3NL6","3NL7","3NL8","3NL9",
                   "3NM0","3NM1","3NM2","3NM3","3LX9"],
     "label": "남성통합암진단세트확장(세만기)", "only_sex": "1"},
    {"all_codes": ["3NM4","3NM5","3NM6","3NM7","3NM8","3NM9",
                   "3NN0","3NN1","3NN2","3NN3","3NN4","3NN5","3LY9"],
     "label": "여성통합암진단세트확장(세만기)", "only_sex": "2"},
    {"all_codes": ["3MW6", "3MW7", "3NB9"], "label": "심혈관질환세부3종"},
    {"all_codes": ["3ND6"], "label": "간병인요양병원"},
    {"all_codes": ["2DWB", "2DWF", "2DUG", "2DUL"], "label": "유사암주요치료비Ⅲ상급4종"},
    {"all_codes": ["2AQZ","2AQP","2FW0","2FW1","2FW2","2ARA","2AQQ"], "label": "하이클래스암7종"},
    *[
        {"all_codes": [cd], "label": "동시가입불가_단독",
         "zero_cds": ["3SN6","3SR8","3LP2","3ND2","3ND3","3LP3"]}
        for cd in ["3LP2", "3ND2", "3ND3", "3LP3"]
    ],
]


def parse_coenroll_group(skip_msg: str) -> tuple[list[str], str]:
    msg = re.sub(r"^[가-힣\s]+해결\s*불가\s*:\s*", "", skip_msg)
    m169 = re.search(rf"169D({CODE_RE})\d+", msg)
    if m169 and "최저가입금액" in msg:
        return [m169.group(1)], "최저금액제한"

    m = re.search(r"피보험자\s+([A-Z0-9]{4,})\[", msg)
    if not m:
        return [], "기타"
    codes_str = m.group(1)
    after = msg[m.end():]

    if   after.startswith("동시가입불가"): return [], "기타"
    elif after.startswith("동시가"):       gtype = "동시가입"
    elif after.startswith("세트가"):       gtype = "세트가입"
    elif after.startswith("필수가"):       gtype = "필수가입"
    elif after.startswith("가입불가"):     gtype = "가입불가"
    else:                                  return [], "기타"

    codes = [codes_str[i:i+4] for i in range(0, len(codes_str), 4)
             if len(codes_str[i:i+4]) == 4]
    if gtype == "가입불가":
        if "남성의 경우" in msg: return codes, "여성전용"
        if "여성의 경우" in msg: return codes, "남성전용"
        return codes, "성별제한"
    return codes, gtype


def load_skipped_groups(prod_cd: str) -> list[dict]:
    fname = "coverage_amounts_세만기.json" if prod_cd == "169D" else "coverage_amounts.json"
    f = ROOT / fname
    if not f.exists():
        return []
    data = json.loads(f.read_text(encoding="utf-8"))
    skipped = [r for r in data if r.get("skip") and "인자값 오류" not in r.get("skip", "")]

    group_map: dict[frozenset, dict] = {}
    ungrouped: list[dict] = []
    for r in skipped:
        codes, gtype = parse_coenroll_group(r.get("skip", ""))
        if not codes:
            ungrouped.append(r)
            continue
        key = frozenset(codes)
        if key not in group_map:
            only_sex = "2" if gtype == "여성전용" else ("1" if gtype == "남성전용" else None)
            group_map[key] = {
                "all_codes": codes, "group_type": gtype,
                "only_sex": only_sex, "targets": [],
            }
        targets = group_map[key]["targets"]
        if not any(t[0] == r["cd"] for t in targets):
            targets.append((r["cd"], r["nm"]))

    groups = list(group_map.values())
    code_to_group = {cd: grp for grp in groups for cd in grp["all_codes"]}
    for r in ungrouped:
        cd, skip = r["cd"], r.get("skip", "")
        if "max" in skip.lower() and "갱신형" not in r.get("nm", "") and cd in code_to_group:
            grp = code_to_group[cd]
            if not any(t[0] == cd for t in grp["targets"]):
                grp["targets"].append((cd, r["nm"]))

    for r in ungrouped:
        codes, gtype = parse_coenroll_group(r.get("skip", ""))
        if gtype == "최저금액제한" and codes:
            groups.append({
                "all_codes": codes, "group_type": "최저금액제한",
                "only_sex": None, "targets": [(r["cd"], r["nm"])], "min_amt": 50,
            })

    추가 = 추가_그룹_169D if prod_cd == "169D" else 추가_그룹_167D
    배상책임 = 배상책임_그룹 if prod_cd == "169D" else []
    existing = {cd for grp in groups for cd, _ in grp["targets"]}
    for g in [*배상책임, *추가]:
        targets = [(r["cd"], r["nm"]) for r in data
                   if r["cd"] in g["all_codes"] and r["cd"] not in existing]
        if targets:
            groups.append({
                "all_codes": g["all_codes"], "group_type": g.get("label", "하드코드"),
                "only_sex": g.get("only_sex"), "targets": targets,
                **({"zero_cds": g["zero_cds"]} if "zero_cds" in g else {}),
            })
            existing.update(cd for cd, _ in targets)
    return groups


def extract_payload_amounts(base: dict) -> dict[str, int]:
    req = base.get("request", base)
    result = {}
    for e in _get_elag_inner(req):
        cd, amt = e.get("elagClsCd"), e.get("elagWonInsdAmt")
        if cd and amt and float(amt or 0) > 0:
            result[cd] = int(float(amt))
    return result


def collect_skipped_groups(
    session, prod: Product,
    ages: list[int], sexes: list[tuple[str, str]],
    groups: list[dict], payload_amts: dict[str, int],
) -> list[dict]:
    rows: list[dict] = []
    log.info("▶ [%s] 스킵 그룹 수집: %d그룹", prod.label, len(groups))

    for gi, grp in enumerate(groups, 1):
        all_codes  = grp["all_codes"]
        targets    = grp["targets"]
        gtype      = grp["group_type"]
        only_sex   = grp["only_sex"]
        target_cds = {cd for cd, _ in targets}
        min_amt    = grp.get("min_amt")

        group_amts = {cd: (min_amt or payload_amts.get(cd, 1000)) for cd in all_codes}
        for cd in grp.get("zero_cds", []):
            if cd not in target_cds and cd in payload_amts:
                group_amts[cd] = 0

        tag = ",".join(all_codes[:3]) + ("..." if len(all_codes) > 3 else "")
        log.info("  [%2d/%d] %s [%s]  대상:%d", gi, len(groups), gtype, tag, len(targets))

        group_skip = False
        for period in prod.periods:
            if group_skip:
                break
            period_nm = PERIOD_LABEL.get(period, period)
            for age in ages:
                if group_skip:
                    break
                sex_list = [(s, n) for s, n in sexes if only_sex is None or s == only_sex]
                for sex_cd, sex_nm in sex_list:
                    effective = dict(group_amts)
                    for attempt in range(5):
                        payload = make_payload(prod.payload, BIRTH[age], sex_cd, period, effective)
                        try:
                            resp = call_api_with_retry(session, payload)
                            parsed = parse_response(resp, age, sex_nm, period_nm)
                        except HiApiError as e:
                            r = recover_from_api_error(e, effective, target_cds, payload_amts)
                            if r.fatal:
                                log.warning("    → %s, 그룹 스킵", r.message)
                                group_skip = True
                                break
                            if r.removed and attempt < 4:
                                continue
                            log.warning("    %s %s세 %s: %s", period_nm, age, sex_nm, e.text[:100])
                            break
                        except Exception as e:        # noqa: BLE001
                            log.error("    %s %s세 %s: 실패 %s", period_nm, age, sex_nm, e)
                            break

                        for item in parsed:
                            if item["담보코드"] in target_cds:
                                item["상품"] = prod.label
                                item["스킵사유"] = gtype
                                rows.append(item)
                        break
                    time.sleep(SLEEP_OK * 0.6)
    log.info("  → 스킵 그룹 수집 완료: %d행", len(rows))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────────────────────
def _visual_width(s: str) -> int:
    """한글·CJK 문자 시각폭 2로 계산."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s or ""))


def autofit(ws) -> None:
    for col in ws.columns:
        mx = max(_visual_width(c.value) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(mx + 4, 50)


COLS_MAIN = ["상품", "나이", "성별", "납입기간", "담보코드", "담보명",
             "담보보험료", "기본보험료", "총납입보험료"]
COLS_SKIP = ["상품", "나이", "성별", "납입기간", "담보코드", "담보명",
             "스킵사유", "담보보험료", "기본보험료", "총납입보험료"]


def save_excel(all_rows: list[dict], skip_rows: list[dict]) -> Path:
    out = ROOT / f"hi_보험료_{TODAY}.xlsx"
    df_main = pd.DataFrame(all_rows)
    df_main = df_main[[c for c in COLS_MAIN if c in df_main.columns]]
    df_skip = pd.DataFrame(skip_rows) if skip_rows else pd.DataFrame(columns=COLS_SKIP)
    if not df_skip.empty:
        df_skip = df_skip[[c for c in COLS_SKIP if c in df_skip.columns]]

    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df_main.to_excel(w, index=False, sheet_name="보험료")
        autofit(w.sheets["보험료"])
        df_skip.to_excel(w, index=False, sheet_name="스킵담보")
        autofit(w.sheets["스킵담보"])

    log.info("✅ %s 저장 (보험료:%d행 / 스킵담보:%d행)", out.name, len(df_main), len(df_skip))
    print(df_main.groupby(["상품", "납입기간", "성별"])["담보보험료"].count()
          .rename("담보수").to_string())
    if not df_skip.empty:
        print("\n[스킵담보 수집 현황]")
        print(df_skip.groupby(["상품", "스킵사유", "담보코드"])["담보보험료"].count()
              .rename("행수").to_string())
    return out


def print_test_result(resp: dict) -> None:
    data   = resp.get("data", resp)
    common = data.get("ltapcommonVO", {})
    elag   = extract_elag_list(data)
    print("\n" + "=" * 60)
    print(f"  총납입보험료: {float(common.get('insrdPrem', 0) or 0):>12,.0f}원")
    print(f"  기본보험료:   {float(common.get('premBasPrem', 0) or 0):>12,.0f}원")
    print(f"\n  {'담보코드':<8} {'담보명':<35} {'보험료':>10}")
    print("  " + "-" * 55)
    for e in elag:
        cd  = e.get("elagClsCd", "")
        nm  = e.get("elagElpaNm", cd)
        prm = float(e.get("tpprPrem", 0) or 0)
        print(f"  {cd:<8} {nm:<35} {prm:>10,.0f}원")
    print("=" * 60 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# 캡처 가이드 (모든 4종 파일명 안내)
# ─────────────────────────────────────────────────────────────────────────────
CAPTURE_GUIDE_TMPL = """
[현대해상 가격공시실 페이로드 캡처 가이드]

1. https://www.hi.co.kr/serviceAction.do 접속
2. F12 → Console 에 아래 코드 붙여넣기 (PRODUCT_FILENAME 변수만 상품별로 변경)
3. 상품 선택 → 보험료 산출 클릭 → JSON 자동 다운로드

(function() {{
  // ↓↓↓ 상품 바꿀 때마다 이 줄만 수정 ↓↓↓
  const PRODUCT_FILENAME = 'hi_payload_연만기_1종.json';

  const _open = XMLHttpRequest.prototype.open;
  const _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) {{ this._url = u; return _open.apply(this, arguments); }};
  XMLHttpRequest.prototype.send = function(body) {{
    if (this._url && this._url.includes('ajax.xhi') && body) {{
      try {{
        const obj = JSON.parse(body);
        if ((obj.header || {{}}).tranId !== '{tran_id}') return _send.apply(this, arguments);
      }} catch(e) {{}}
      const blob = new Blob([body], {{type:'application/json'}});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = PRODUCT_FILENAME;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      console.log('저장됨 → ' + PRODUCT_FILENAME);
    }}
    return _send.apply(this, arguments);
  }};
  console.log('인터셉터 설치 완료 (tranId={tran_id}). 보험료 산출 클릭하세요.');
}})();

▶ 상품별 저장 파일명:
{file_table}
"""


def show_capture_guide() -> None:
    table = "\n".join(f"  {label:35s} → {fname}"
                      for fname, (label, _) in PAYLOAD_FILES.items())
    print(CAPTURE_GUIDE_TMPL.format(tran_id=TRAN_ID, file_table=table))


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="현대해상 보험료 수집기")
    p.add_argument("--test", action="store_true", help="첫 상품 1조합만 호출 후 출력")
    p.add_argument("--skip-test", action="store_true",
                   help="스킵그룹만 1조합 테스트")
    p.add_argument("--show-capture-guide", action="store_true",
                   help="브라우저 인터셉터 가이드 출력")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG 로그")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    if args.show_capture_guide:
        show_capture_guide()
        return

    payloads = load_payloads()

    log.info("▶ 세션 초기화...")
    session = init_session()

    # 모드별 ages/sexes 결정
    if args.test or args.skip_test:
        ages, sexes = [50], [("1", "남")]
    else:
        ages, sexes = ALL_AGES, ALL_SEXES

    if args.test:
        prod = payloads[0]
        period = prod.periods[1] if len(prod.periods) > 1 else prod.periods[0]
        log.info("🧪 테스트: [%s] %s 50세 남", prod.label, PERIOD_LABEL.get(period, period))
        resp = call_api(session, make_payload(prod.payload, BIRTH[50], "1", period))
        print_test_result(resp)
        return

    if args.skip_test:
        prod = payloads[0]
        period = prod.periods[0]
        prod_cd = prod.payload.get("request", prod.payload).get("ltapcommonVO", {}).get("inagProdCd", "167D")
        groups = load_skipped_groups(prod_cd)
        amts = extract_payload_amounts(prod.payload)
        log.info("🧪 스킵그룹 테스트: [%s] %s 50세 남 (그룹 %d, 대상 %d)",
                 prod.label, PERIOD_LABEL.get(period, period),
                 len(groups), sum(len(g["targets"]) for g in groups))
        prod_only_period = Product(prod.label, [period], prod.payload)
        rows = collect_skipped_groups(session, prod_only_period, ages, sexes, groups, amts)
        if rows:
            df = pd.DataFrame(rows)
            print(df[[c for c in ["담보코드","담보명","스킵사유","담보보험료"] if c in df.columns]]
                  .to_string(index=False))
        else:
            log.warning("수집된 행 없음")
        return

    # 전체 수집
    all_rows: list[dict] = []
    skip_rows: list[dict] = []
    for prod in payloads:
        prod_cd = prod.payload.get("request", prod.payload).get("ltapcommonVO", {}).get("inagProdCd", "167D")
        extra = load_coverage_amounts(prod_cd)
        if extra:
            log.info("▶ [%s] coverage_amounts: %d개 (%s)", prod.label, len(extra), prod_cd)
        all_rows.extend(collect_one(session, prod, ages, sexes, extra))

        groups = load_skipped_groups(prod_cd)
        if groups:
            amts = extract_payload_amounts(prod.payload)
            skip_rows.extend(collect_skipped_groups(session, prod, ages, sexes, groups, amts))

    if not all_rows:
        log.warning("수집된 데이터 없음")
        return

    all_rows = supplement_renewal_coverages(all_rows)
    save_excel(all_rows, skip_rows)


if __name__ == "__main__":
    main()
