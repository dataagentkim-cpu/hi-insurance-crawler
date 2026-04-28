"""
담보별 유효 금액 및 의존성 탐색 (1회 실행 후 coverage_amounts.json 저장)
"""
import copy, json, re, sys, time
from pathlib import Path
import requests
from hi_collect import (
    make_payload, init_session, call_api, BIRTH, extract_elag_list,
    BASE, HEADERS, API_URL
)

import sys as _sys

# 인자로 상품 지정 가능: python3 discover_coverages.py 세만기
if len(_sys.argv) > 1 and "세만기" in _sys.argv[1]:
    AMOUNTS_FILE = Path(__file__).parent / "coverage_amounts_세만기.json"
    BASE_PAYLOAD  = Path(__file__).parent / "hi_payload_세만기_1종.json"
    TEST_PERIOD   = "Y020A100"
    IS_SEMAKI     = True
else:
    AMOUNTS_FILE = Path(__file__).parent / "coverage_amounts.json"
    BASE_PAYLOAD  = Path(__file__).parent / "hi_payload_연만기_2종.json"
    TEST_PERIOD   = "Y030Y030"
    IS_SEMAKI     = False

# 50세 남 기준으로 탐색
TEST_BIRTH = BIRTH[50]
TEST_SEX   = "1"

BASE_CDS = {"3AR4", "3LA8"}   # 기본 선택된 담보 (변경 안 함)


def parse_error(msg: str) -> dict:
    """오류 메시지를 파싱해서 구조화된 정보 반환."""
    info = {"raw": msg.strip()}
    m_min = re.search(r'최소\s+(\d+)천원', msg)
    m_max = re.search(r'최대\s+(\d+)천원', msg)
    m_set = re.search(r'\[세트가입\]', msg)
    m_req = re.search(r'\[필수가입\]', msg)
    m_code = re.search(r'피보험자\s+(\w+?)(\w{4})(\d{2})', msg)

    m_fixed = re.search(r'(\d+)천원만\s*가입', msg)
    if m_min:   info["min_amt"]   = int(m_min.group(1))
    if m_max:   info["max_amt"]   = int(m_max.group(1))
    if m_fixed: info["fixed_amt"] = int(m_fixed.group(1))
    if m_set: info["type"] = "세트가입"
    if m_req: info["type"] = "필수가입"
    return info


def try_add_coverage(session, base_payload_p0, cd, amt, extra_cds: dict = None, target_period: str = None):
    """
    cd를 amt (천원)으로 추가해서 API 호출. extra_cds = {cd: amt} 추가 담보.
    target_period: 지정 시 cd의 paydInsdPeriod를 이 값으로 오버라이드 (Z999Y-형 담보용)
    반환: (premium: float | None, error_info: dict | None)
    """
    p = copy.deepcopy(base_payload_p0)
    req = p.get("request", p)
    req_inner = req.get("elagInfoList", [])[0].get("elagInfoList", [])

    # 세만기형: 빈 paydInsdPeriod → TEST_PERIOD로 채움
    if "A" in str(TEST_PERIOD):
        for e in req_inner:
            ep = e.get("paydInsdPeriod", "")
            if not ep or ep == "선택안됨":
                e["paydInsdPeriod"] = TEST_PERIOD

    target_cds = {cd: amt}
    if extra_cds:
        target_cds.update(extra_cds)

    for e in req_inner:
        c = e.get("elagClsCd")
        if c in target_cds:
            e["elagWonInsdAmt"] = target_cds[c]
            if target_period and c == cd:
                e["paydInsdPeriod"] = target_period

    # 연결 재시도 (최대 3회)
    for attempt in range(3):
        try:
            resp = call_api(session, p)
            break
        except Exception as conn_err:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
            else:
                return None, parse_error(f"ConnectionError: {conn_err}")
    else:
        return None, parse_error("Max connection retries")

    rs   = resp.get("responseStatus", {})
    if rs.get("exceptionOccurred"):
        msg  = rs.get("exception", {}).get("message", "")
        return None, parse_error(msg)

    data     = resp.get("data", resp)
    inner_r  = data.get("elagInfoList", [])[0].get("elagInfoList", [])
    e_target = next((e for e in inner_r if e.get("elagClsCd") == cd), {})
    prem     = float(e_target.get("tpprPrem", 0) or 0)
    return prem, None


def discover_one(session, base_p0, cd, nm, all_cds_set):
    """
    cd 하나에 대해 유효 금액 탐색.
    반환: {"cd": ..., "nm": ..., "amt": ..., "prem": ..., "extras": {...}, "skip": ...}
    """
    result = {"cd": cd, "nm": nm, "amt": None, "prem": None, "extras": {}, "skip": None}

    # 1단계: amt=100 으로 시작
    amt = 100
    extras = {}
    z_period = None  # 세만기 Z999Y-형 period (납입지원/면제 계열 담보용)

    for attempt in range(8):
        prem, err = try_add_coverage(session, base_p0, cd, amt, extras, target_period=z_period)
        time.sleep(0.3)

        if err is None:
            # 성공
            result["amt"]    = amt
            result["prem"]   = prem
            result["extras"] = extras
            return result

        # 오류 분석
        raw = err.get("raw", "")

        # extras 중 특정 코드에 대한 금액 오류 처리 (먼저 확인)
        if extras and ("min_amt" in err or "max_amt" in err or "fixed_amt" in err):
            codes_in_err = re.findall(r'([1-9][A-Z0-9]{3})', raw)
            extra_cd = next((c for c in codes_in_err if c in extras and c != cd), None)
            if extra_cd:
                if "fixed_amt" in err:
                    extras[extra_cd] = err["fixed_amt"]
                elif "min_amt" in err and extras[extra_cd] < err["min_amt"]:
                    extras[extra_cd] = err["min_amt"]
                elif "max_amt" in err and extras[extra_cd] > err["max_amt"]:
                    extras[extra_cd] = max(1, err["max_amt"])
                continue

        if "fixed_amt" in err:
            amt = err["fixed_amt"]
            continue

        if "min_amt" in err and amt < err["min_amt"]:
            amt = err["min_amt"]
            continue

        if "max_amt" in err and amt > err["max_amt"]:
            amt = max(1, err["max_amt"])
            continue

        # 세만기형: 납만기 기본담보 납기 불일치 → Z999Y 형식으로 재시도
        if "납만기" in raw and "A" in str(TEST_PERIOD) and z_period is None:
            pay_yr = TEST_PERIOD.split("A")[0]
            z_period = "Z999" + pay_yr
            continue

        # 세트가입 처리 - 에러에서 코드 추출 (연속된 코드도 파싱)
        if err.get("type") == "세트가입":
            codes_in_msg = re.findall(r'([1-9][A-Z0-9]{3})', raw)
            set_cds = list(dict.fromkeys(
                c for c in codes_in_msg if c in all_cds_set and c != cd and c not in BASE_CDS
            ))
            if set_cds and not extras:
                extras = {c: 100 for c in set_cds[:10]}
                continue
            result["skip"] = f"세트가입 해결 불가: {raw[:60]}"
            return result

        # 필수가입 처리 - 선행 담보 찾기 (연속된 코드도 파싱)
        if err.get("type") == "필수가입":
            codes_in_msg = re.findall(r'([1-9][A-Z0-9]{3})', raw)
            prereqs = list(dict.fromkeys(
                c for c in codes_in_msg if c in all_cds_set and c != cd and c not in BASE_CDS
            ))
            if prereqs and not extras:
                extras = {c: 100 for c in prereqs[:10]}
                continue
            result["skip"] = f"필수가입 해결 불가: {raw[:60]}"
            return result

        # 5의 배수 요구 등
        if "5" in raw[:30] and attempt < 4:
            amt = max(10, (amt // 5) * 5) if amt % 5 else amt + 5
            continue

        # 기타 오류
        result["skip"] = raw[:80]
        return result

    result["skip"] = "max attempts"
    return result


def main():
    print(f"▶ 세션 초기화... ({'세만기' if IS_SEMAKI else '연만기'} 모드)")
    session = init_session()

    with open(BASE_PAYLOAD, encoding="utf-8") as f:
        base = json.load(f)

    p0 = make_payload(base, TEST_BIRTH, TEST_SEX, TEST_PERIOD)

    # 담보 목록 가져오기
    resp0 = call_api(session, p0)
    data0 = resp0.get("data", resp0)
    all_cov = data0.get("elagInfoList", [])[0].get("elagInfoList", [])
    all_cds_set = {e.get("elagClsCd") for e in all_cov}

    to_discover = [e for e in all_cov if e.get("elagClsCd") not in BASE_CDS]
    print(f"탐색할 담보: {len(to_discover)}개")

    # 기존 결과 로드 (재개 가능)
    existing = {}
    if AMOUNTS_FILE.exists():
        existing = {r["cd"]: r for r in json.loads(AMOUNTS_FILE.read_text(encoding="utf-8"))}
        print(f"기존 결과 {len(existing)}개 로드 (건너뜀)")

    results = list(existing.values())
    ok = len([r for r in results if r.get("amt")])
    skipped = len([r for r in results if r.get("skip")])

    for i, e in enumerate(to_discover):
        cd = e.get("elagClsCd")
        nm = e.get("elagElpaNm", cd)[:40]
        if cd in existing:
            continue

        n = i + 1
        res = discover_one(session, p0, cd, nm, all_cds_set)
        results.append(res)

        if res.get("skip"):
            skipped += 1
            print(f"[{n:>3}/{len(to_discover)}] SKIP  {cd} {nm[:30]}: {res['skip'][:50]}")
        else:
            ok += 1
            print(f"[{n:>3}/{len(to_discover)}] OK    {cd} {nm[:30]}: {res['amt']}천원 → {res['prem']}원")

        # 중간 저장 (10개마다)
        if n % 10 == 0:
            AMOUNTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

        time.sleep(1.0)  # 서버 부하 방지

    AMOUNTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 완료  성공:{ok}  스킵:{skipped}")
    print(f"   결과 저장: {AMOUNTS_FILE.name}")


if __name__ == "__main__":
    main()
