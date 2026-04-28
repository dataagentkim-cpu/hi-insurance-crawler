"""
현대해상 가격공시실 보험료 수집기
https://www.hi.co.kr/serviceAction.do

[사용법]
1. https://www.hi.co.kr/serviceAction.do 접속
2. F12 → Network 탭 → 상품 선택 후 보험료 산출 클릭
3. Console에 아래 인터셉터 코드 붙여넣고 → 보험료 산출 클릭
4. 다운로드된 JSON을 해당 상품 파일명으로 저장
5. python3 hi_collect.py            # 전체 수집
   python3 hi_collect.py --test     # 1회 테스트
"""

import copy, json, re, sys, time
from datetime import datetime
from pathlib import Path
try:
    from curl_cffi import requests
except ImportError:
    import requests
import pandas as pd

TEST_MODE = "--test" in sys.argv

TODAY = datetime.today().strftime("%Y%m%d")
BASE  = "https://www.hi.co.kr"
API_URL = f"{BASE}/ajax.xhi"

# 연만기갱신형 납입기간 옵션 — 5년/25년납은 기본계약 미지원으로 제외
PAYD_PERIODS_연만기 = [
    "Y010Y010",  # 10년
    "Y015Y015",  # 15년
    "Y020Y020",  # 20년
    "Y030Y030",  # 30년
]
# 세만기형 1종 납입기간 옵션
PAYD_PERIODS_세만기_1종 = [
    "Y010A090",  # 10년납 90세만기
    "Y010A100",  # 10년납 100세만기
    "Y015A090",  # 15년납 90세만기
    "Y015A100",  # 15년납 100세만기
    "Y020A090",  # 20년납 90세만기
    "Y020A100",  # 20년납 100세만기
    "Y025A090",  # 25년납 90세만기
    "Y025A100",  # 25년납 100세만기
    "Y030A100",  # 30년납 100세만기
]
# 세만기형 2종 납입기간 옵션 — 10년납/15년납은 기본계약 미지원으로 제외
PAYD_PERIODS_세만기_2종 = [
    "Y020A090",  # 20년납 90세만기
    "Y020A100",  # 20년납 100세만기
    "Y025A090",  # 25년납 90세만기
    "Y025A100",  # 25년납 100세만기
    "Y030A100",  # 30년납 100세만기
]

# 상품별 payload 파일 목록 (파일명: (표시명, 납입기간목록))
PAYLOAD_FILES = {
    "hi_payload_연만기_1종.json": ("연만기갱신형 1종(표준형)",           PAYD_PERIODS_연만기),
    "hi_payload_연만기_2종.json": ("연만기갱신형 2종(해약환급금미지급형)", PAYD_PERIODS_연만기),
    "hi_payload_세만기_1종.json": ("세만기형 1종(표준형)",               PAYD_PERIODS_세만기_1종),
    "hi_payload_세만기_2종.json": ("세만기형 2종(해약환급금미지급형)",     PAYD_PERIODS_세만기_2종),
}

PERIOD_LABEL = {
    "Y005Y005": "5년",  "Y010Y010": "10년", "Y015Y015": "15년",
    "Y020Y020": "20년", "Y025Y025": "25년", "Y030Y030": "30년",
    # 세만기형: 납입기간_만기나이
    "Y010A090": "10년납_90세만기", "Y010A100": "10년납_100세만기",
    "Y015A090": "15년납_90세만기", "Y015A100": "15년납_100세만기",
    "Y020A090": "20년납_90세만기", "Y020A100": "20년납_100세만기",
    "Y025A090": "25년납_90세만기", "Y025A100": "25년납_100세만기",
    "Y030A100": "30년납_100세만기",
}

# 보험나이 기준 생년월일
BIRTH = {
    30: "19960701", 35: "19910701", 40: "19860701",
    45: "19810701", 50: "19760701", 55: "19710701",
    60: "19660701", 65: "19610701", 70: "19560701",
}
AGES  = [50] if TEST_MODE else [30, 35, 40, 45, 50, 55, 60, 65, 70]
SEXES = [("1", "남")] if TEST_MODE else [("1", "남"), ("2", "여")]

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/serviceAction.do",
    "Origin": BASE,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

CAPTURE_GUIDE = """
Console에 아래 코드를 붙여넣고 보험료 조회 버튼을 클릭하면 JSON이 자동 다운로드됩니다:

(function() {
  const _open = XMLHttpRequest.prototype.open;
  const _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this._url = u; return _open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(body) {
    if (this._url && this._url.includes('ajax.xhi') && body) {
      try {
        const obj = JSON.parse(body);
        const hdr = obj.header || {};
        if (hdr.tranId !== 'HHCA0030M07S') return _send.apply(this, arguments);
      } catch(e) {}
      const blob = new Blob([body], {type:'application/json'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'hi_payload_연만기_1종.json';  // ← 저장할 파일명 변경
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      console.log('저장됨 (HHCA0030M07S)');
    }
    return _send.apply(this, arguments);
  };
  console.log('인터셉터 설치 완료 → 보험료 산출 클릭');
})();

* tranId=HHCA0030M07S 요청만 저장됩니다 (보험료 산출 단계)
"""


def load_payloads() -> list[tuple[str, list[str], dict]]:
    """(표시명, 납입기간목록, payload) 목록 반환. 없는 파일은 경고 후 스킵."""
    base_dir = Path(__file__).parent
    found, missing = [], []

    for fname, (label, periods) in PAYLOAD_FILES.items():
        fpath = base_dir / fname
        if fpath.exists():
            found.append((label, periods, json.loads(fpath.read_text(encoding="utf-8"))))
        else:
            missing.append(fname)

    if missing:
        print(f"⚠️  아직 캡처 안 된 파일: {', '.join(missing)}")
        print("   → 아래 가이드로 각 상품 payload 캡처 후 저장하세요")

    if not found:
        print("❌ payload 파일이 하나도 없습니다.")
        print(CAPTURE_GUIDE)
        print("\n상품별 저장 파일명:")
        for fname, (label, _) in PAYLOAD_FILES.items():
            print(f"  {label:35s} → {fname}")
        sys.exit(1)

    return found


def load_coverage_amounts(prod_cd: str = "167D") -> dict[str, int]:
    """담보별 유효금액 로드. 연만기(167D) → coverage_amounts.json, 세만기(169D) → coverage_amounts_세만기.json.
    세트가입/필수가입 extras도 포함해서 서버 세트 조건 위반을 방지한다."""
    fname = "coverage_amounts_세만기.json" if prod_cd == "169D" else "coverage_amounts.json"
    f = Path(__file__).parent / fname
    if not f.exists():
        return {}
    data = json.loads(f.read_text(encoding="utf-8"))
    # 단순 담보(extras={})만 포함 — 세트/필수가입 의존성 있는 복합 담보는 제외
    # (복합 담보를 모두 포함하면 동시가입불가·세트가입 제약 간 연쇄 충돌 발생)
    amounts = {}
    for r in data:
        if r.get("amt") and not r.get("skip") and not r.get("extras"):
            amounts[r["cd"]] = r["amt"]
    return amounts


def make_payload(base: dict, birth: str, sex_cd: str, payd_period: str | None = None,
                 extra_amounts: dict[str, int] | None = None) -> dict:
    """나이·성별·납입기간을 교체한 payload 반환. extra_amounts = {코드: 천원금액}"""
    p = copy.deepcopy(base)
    reg_no = birth[2:] + sex_cd + "000000"

    def patch_item(item: dict):
        if "ptyBrdt"   in item: item["ptyBrdt"]   = birth
        if "ptySxdsCd" in item: item["ptySxdsCd"] = sex_cd
        if "ptyRegNo"  in item: item["ptyRegNo"]  = reg_no

    def patch_any(obj):
        if isinstance(obj, dict):
            patch_item(obj)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    patch_item(item)

    req = p.get("request", p)
    patch_any(req.get("insurdInfo2List", []))
    patch_any(req.get("insurdInfoList",  []))
    patch_any(req.get("insurdList",      []))

    lvo = req.get("ltapcommonVO", {})
    for fld in ("ctrtrPtyRegNo", "insrdPtyRegNo"):
        if fld in lvo:
            lvo[fld] = reg_no
    for fld in ("inagInsStDt", "savePremStDt", "premCalDt"):
        if fld in lvo:
            lvo[fld] = TODAY

    if payd_period and "paydInsdPeriod" in lvo:
        lvo["paydInsdPeriod"] = payd_period

    # 세만기형: 담보 항목별 paydInsdPeriod 패치 (ltapcommonVO가 아닌 각 담보에 있음)
    if payd_period and "A" in str(payd_period):
        req_outer = req.get("elagInfoList", [])
        if req_outer:
            req_inner = req_outer[0].get("elagInfoList", []) if isinstance(req_outer[0], dict) else []
            pay_yr = payd_period.split("A")[0]  # "Y020A100" → "Y020"
            for e in req_inner:
                ep = e.get("paydInsdPeriod", "")
                if not ep:
                    continue
                if ep.startswith("Y") and "A" in ep:
                    e["paydInsdPeriod"] = payd_period
                elif ep.startswith("Z") and "Y" in ep:
                    e["paydInsdPeriod"] = "Z999" + pay_yr

    # 추가 담보 금액 적용 (cover_amounts.json 기반)
    if extra_amounts:
        req_outer = req.get("elagInfoList", [])
        if req_outer:
            req_inner = req_outer[0].get("elagInfoList", []) if isinstance(req_outer[0], dict) else []
            is_semaki = payd_period and "A" in str(payd_period)
            pay_yr    = payd_period.split("A")[0] if is_semaki else ""
            for e in req_inner:
                cd = e.get("elagClsCd")
                if cd not in extra_amounts:
                    continue
                e["elagWonInsdAmt"] = extra_amounts[cd]
                # 세만기형: 빈 paydInsdPeriod인 담보에 period 주입
                # 납입지원/면제 계열은 Z999Y 형식, 나머지는 Y형식
                if is_semaki and not e.get("paydInsdPeriod"):
                    nm = e.get("elagElpaNm", "")
                    if "납입지원" in nm or "납입면제" in nm or "납입보장" in nm:
                        e["paydInsdPeriod"] = "Z999" + pay_yr
                    else:
                        e["paydInsdPeriod"] = payd_period

    return p


def init_session():
    try:
        from curl_cffi import requests as cffi_requests
        s = cffi_requests.Session(impersonate="chrome120")
    except ImportError:
        s = requests.Session()
    s.headers.update({"User-Agent": HEADERS["User-Agent"]})
    for attempt in range(3):
        try:
            s.get(f"{BASE}/serviceAction.do", timeout=15)
            return s
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
                continue
            raise
    return s


def call_api(session: requests.Session, payload: dict) -> dict:
    r = session.post(API_URL, json=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def extract_elag_list(data: dict) -> list[dict]:
    outer = data.get("elagInfoList", [])
    if not outer:
        return []
    inner = outer[0].get("elagInfoList", []) if isinstance(outer[0], dict) else []
    return [e for e in inner if float(e.get("tpprPrem", 0) or 0) > 0]


def parse_response(resp: dict, age: int, sex_nm: str, period_label: str) -> list[dict]:
    rs = resp.get("responseStatus", {})
    if rs.get("exceptionOccurred"):
        exc = rs.get("exception", {})
        raise RuntimeError(f"{exc.get('code')} {exc.get('message','')}")

    data      = resp.get("data", resp)
    common    = data.get("ltapcommonVO", {})
    elag_list = extract_elag_list(data)
    total_prem = float(common.get("insrdPrem", 0) or 0)

    rows = []
    for e in elag_list:
        rows.append({
            "나이":       age,
            "성별":       sex_nm,
            "납입기간":   period_label,
            "담보코드":   e.get("elagClsCd", ""),
            "담보명":     e.get("elagElpaNm", e.get("elagClsCd", "")),
            "담보보험료":  float(e.get("tpprPrem", 0) or 0),
            "기본보험료":  float(common.get("premBasPrem", 0) or 0),
            "총납입보험료": total_prem,
        })
    return rows


def print_test_result(resp: dict):
    data      = resp.get("data", resp)
    common    = data.get("ltapcommonVO", {})
    elag_list = extract_elag_list(data)

    print(f"\n{'='*60}")
    print(f"  총납입보험료: {float(common.get('insrdPrem', 0) or 0):>12,.0f}원")
    print(f"  기본보험료:   {float(common.get('premBasPrem', 0) or 0):>12,.0f}원")
    print(f"\n  {'담보코드':<8} {'담보명':<35} {'보험료':>10}")
    print(f"  {'-'*55}")
    for e in elag_list:
        cd  = e.get("elagClsCd", "")
        nm  = e.get("elagElpaNm", cd)
        prm = float(e.get("tpprPrem", 0) or 0)
        print(f"  {cd:<8} {nm:<35} {prm:>10,.0f}원")
    print(f"{'='*60}\n")


def collect_one(session, label: str, periods: list[str], base_payload: dict,
                extra_amounts: dict | None = None) -> list[dict]:
    """하나의 상품에 대해 전체 납입기간×나이×성별 수집."""
    total = len(periods) * len(AGES) * len(SEXES)
    rows  = []
    n     = 0
    print(f"\n▶ [{label}]  납입기간 {len(periods)}종 × {len(AGES)}나이 × {len(SEXES)}성별 = {total}회")
    if extra_amounts:
        print(f"   담보 {len(extra_amounts)}개 금액 설정됨 (coverage_amounts.json)")

    for period in periods:
        period_nm = PERIOD_LABEL.get(period, period)
        for age in AGES:
            for sex_cd, sex_nm in SEXES:
                n += 1
                effective_extra = dict(extra_amounts) if extra_amounts else {}
                for attempt in range(6):
                    payload = make_payload(base_payload, BIRTH[age], sex_cd, period, effective_extra)
                    try:
                        resp = call_api(session, payload)
                        r    = parse_response(resp, age, sex_nm, period_nm)
                        if not r:
                            print(f"  [{n:>3}/{total}] {period_nm} {age}세 {sex_nm}: 담보 없음 (미지원 납입기간?)")
                            break
                        for item in r:
                            item["상품"] = label
                        rows.extend(r)
                        total_prem = r[0]["총납입보험료"]
                        print(f"  [{n:>3}/{total}] {period_nm} {age}세 {sex_nm}  "
                              f"총납입:{int(total_prem):,}원  담보:{len(r)}개")
                        break
                    except RuntimeError as e:
                        err_msg = str(e)
                        if attempt < 5:
                            removed = 0
                            # 동시가입불가: 충돌 쌍에서 두번째 담보 제거
                            if "동시가입불가" in err_msg:
                                for m in re.finditer(
                                    r'([1-9][A-Z0-9]{3})([1-9][A-Z0-9]{3})\[동시가입불가\]', err_msg
                                ):
                                    cd_remove = m.group(2)
                                    if cd_remove in effective_extra:
                                        del effective_extra[cd_remove]
                                        removed += 1
                            # ULT01009/ULT00016: 연령·키구성 제한 담보 제거
                            elif "ULT01009" in err_msg or (
                                "ULT00016" in err_msg and "키구성" in err_msg
                            ):
                                # "담보:XXXX" 패턴 우선, 없으면 첫 매칭 코드
                                m_cd = re.search(r'담보:([1-9][A-Z0-9]{3})', err_msg)
                                candidates = (
                                    [m_cd.group(1)] if m_cd
                                    else re.findall(r'([1-9][A-Z0-9]{3})', err_msg)
                                )
                                for cd_remove in candidates:
                                    if cd_remove in effective_extra:
                                        del effective_extra[cd_remove]
                                        removed += 1
                                        break
                            if removed:
                                print(f"  [{n:>3}/{total}] {period_nm} {age}세 {sex_nm}: "
                                      f"연령/충돌 제한 {removed}개 제외 후 재시도")
                                continue
                        print(f"  [{n:>3}/{total}] {period_nm} {age}세 {sex_nm}: 오류 → {err_msg[:120]}")
                        break
                    except Exception as e:
                        if attempt < 2:
                            print(f"  [{n:>3}/{total}] {period_nm} {age}세 {sex_nm}: 재시도 {e}")
                            time.sleep(3)
                            continue
                        print(f"  [{n:>3}/{total}] {period_nm} {age}세 {sex_nm}: 실패 {e}")
                        break
                time.sleep(0.5)
    return rows


def supplement_renewal_coverages(all_rows: list[dict]) -> list[dict]:
    """세만기 상품의 갱신형 담보(세만기 API 계산불가)를 연만기 결과에서 보완.

    갱신형 특약은 주계약 납입기간과 독립적으로 자체 납입기간/만기 옵션(전기납/10년만기 등)을
    가지므로, 연만기 수집 결과의 모든 납입기간 옵션을 그대로 세만기 행으로 추가한다.
    종류 매핑: 세만기 1종 ↔ 연만기 1종, 세만기 2종 ↔ 연만기 2종
    """
    semaki_path = Path(__file__).parent / "coverage_amounts_세만기.json"
    if not semaki_path.exists():
        return all_rows
    semaki_data = json.loads(semaki_path.read_text(encoding="utf-8"))
    renewal_cds = {r["cd"] for r in semaki_data if r.get("skip") and "인자값 오류" in r["skip"]}
    if not renewal_cds:
        return all_rows

    YEON_TYPE = {
        "연만기갱신형 1종(표준형)":          "1종",
        "연만기갱신형 2종(해약환급금미지급형)": "2종",
    }
    SEMAKI_TYPE = {
        "세만기형 1종(표준형)":          "1종",
        "세만기형 2종(해약환급금미지급형)": "2종",
    }

    # 연만기 갱신형 행 인덱스: (종류, 나이, 성별, 납입기간_label, 담보코드) → row
    yeon_idx: dict[tuple, dict] = {}
    for row in all_rows:
        t = YEON_TYPE.get(row.get("상품"))
        if t and row.get("담보코드") in renewal_cds:
            key = (t, row["나이"], row["성별"], row["납입기간"], row["담보코드"])
            yeon_idx[key] = row

    if not yeon_idx:
        return all_rows

    # 연만기에서 수집된 납입기간 옵션 목록 (정렬)
    yeon_periods = sorted({row["납입기간"] for row in all_rows if row.get("상품") in YEON_TYPE})

    # 세만기 (상품, 나이, 성별) 조합 수집
    semaki_combos: set[tuple] = set()
    for row in all_rows:
        if row.get("상품") in SEMAKI_TYPE:
            semaki_combos.add((row["상품"], row["나이"], row["성별"]))

    new_rows = []
    for (prod, age, sex) in sorted(semaki_combos):
        t = SEMAKI_TYPE[prod]
        for period in yeon_periods:
            for cd in sorted(renewal_cds):
                key = (t, age, sex, period, cd)
                if key in yeon_idx:
                    src = yeon_idx[key]
                    new_rows.append({
                        "상품":       prod,
                        "나이":       age,
                        "성별":       sex,
                        "납입기간":   period,   # 갱신형 특약 자체 납입기간 옵션
                        "담보코드":   cd,
                        "담보명":     src["담보명"],
                        "담보보험료":  src["담보보험료"],
                        "기본보험료":  src["기본보험료"],
                        "총납입보험료": src["총납입보험료"],
                    })

    if new_rows:
        print(f"▶ 세만기 갱신형 담보 보완: {len(new_rows)}행 추가"
              f" (연만기 {len(yeon_periods)}개 납입기간 옵션 × {len(renewal_cds)}개 담보)")
    return all_rows + new_rows


배상책임_그룹 = [
    # ULT31117: 배상책임 담보는 반드시 묶음 가입
    {"all_codes": ["1ZRB", "1ZRC", "1ZRD"], "label": "배상책임(누수포함)"},
    {"all_codes": ["1ZRE", "1ZRF"],          "label": "배상책임(누수제외)"},
]


def parse_coenroll_group(skip_msg: str) -> tuple[list[str], str]:
    """스킵 메시지에서 동시가입 필요 코드 목록과 타입 반환."""
    # 접두 레이블 제거 ("세트가입 해결 불가: ", "필수가입 해결 불가: " 등)
    msg = re.sub(r'^[가-힣\s]+해결\s*불가\s*:\s*', '', skip_msg)

    # 169D 접두 코드 (개별 담보 최저금액 에러)
    # 예: "169D3LT500담보명 : 최저가입금액은 50"
    m169 = re.search(r'169D([1-9A-Z][A-Z0-9]{3})\d+', msg)
    if m169 and "최저가입금액" in msg:
        return [m169.group(1)], "최저금액제한"

    m = re.search(
        r'피보험자\s+([A-Z0-9]{4,})\[(동시가입|세트가입|필수가입|가입불가)\]',
        msg
    )
    if not m:
        return [], "기타"
    codes_str = m.group(1)
    gtype     = m.group(2)
    codes = [codes_str[i:i+4] for i in range(0, len(codes_str), 4) if len(codes_str[i:i+4]) == 4]
    if gtype == "가입불가":
        if "남성의 경우" in msg:
            return codes, "여성전용"
        if "여성의 경우" in msg:
            return codes, "남성전용"
        return codes, "성별제한"
    return codes, gtype


def load_skipped_groups(prod_cd: str = "167D") -> list[dict]:
    """
    스킵된 담보를 동시가입 그룹별로 정리.
    갱신형(인자값 오류)은 제외 — 이미 연만기로 보완됨.
    Returns list of {all_codes, targets, group_type, only_sex}
    """
    fname = "coverage_amounts_세만기.json" if prod_cd == "169D" else "coverage_amounts.json"
    f = Path(__file__).parent / fname
    if not f.exists():
        return []
    data = json.loads(f.read_text(encoding="utf-8"))

    skipped = [
        r for r in data
        if r.get("skip") and "인자값 오류" not in r.get("skip", "")
    ]

    group_map: dict[frozenset, dict] = {}
    ungrouped: list[dict] = []

    for r in skipped:
        cd       = r["cd"]
        skip_msg = r.get("skip", "")
        codes, gtype = parse_coenroll_group(skip_msg)

        if not codes:
            ungrouped.append(r)
            continue

        key = frozenset(codes)
        if key not in group_map:
            only_sex = "2" if gtype == "여성전용" else ("1" if gtype == "남성전용" else None)
            group_map[key] = {
                "all_codes":  codes,
                "group_type": gtype,
                "only_sex":   only_sex,
                "targets":    [],
            }
        targets = group_map[key]["targets"]
        if not any(t[0] == cd for t in targets):
            targets.append((cd, r["nm"]))

    groups = list(group_map.values())

    # 최저금액 제한 담보: 개별 수집, 금액 50 고정
    for r in ungrouped:
        codes, gtype = parse_coenroll_group(r.get("skip", ""))
        if gtype == "최저금액제한" and codes:
            groups.append({
                "all_codes":  codes,
                "group_type": "최저금액제한",
                "only_sex":   None,
                "targets":    [(r["cd"], r["nm"])],
                "min_amt":    50,
            })
        # 동시가입불가(1인1계약), max attempts, 필수가입 등 → 현재는 스킵

    # ULT31117 배상책임 그룹 — skip 메시지에 코드가 없어서 하드코드
    for grp_info in 배상책임_그룹:
        target_cds = grp_info["all_codes"]
        # data에 해당 코드가 있는지 확인
        targets = [(r["cd"], r["nm"]) for r in data if r["cd"] in target_cds]
        if targets:
            groups.append({
                "all_codes":  target_cds,
                "group_type": "배상책임",
                "only_sex":   None,
                "targets":    targets,
            })

    return groups


def extract_payload_amounts(base_payload: dict) -> dict[str, int]:
    """Base payload에서 담보별 가입금액 추출 (elagWonInsdAmt, 천원 단위)."""
    req   = base_payload.get("request", base_payload)
    outer = req.get("elagInfoList", [])
    if not outer:
        return {}
    inner = outer[0].get("elagInfoList", []) if isinstance(outer[0], dict) else []
    result = {}
    for e in inner:
        cd  = e.get("elagClsCd")
        amt = e.get("elagWonInsdAmt")
        if cd and amt and float(amt or 0) > 0:
            result[cd] = int(float(amt))
    return result


def collect_skipped_groups(
    session,
    label: str,
    periods: list[str],
    base_payload: dict,
    groups: list[dict],
    payload_amounts: dict[str, int],
) -> list[dict]:
    """
    동시가입/세트가입 그룹 담보 수집.
    그룹 내 전체 코드를 페이로드에 함께 넣어 개별 보험료를 추출.
    여성전용/남성전용은 해당 성별만 수집.
    """
    rows = []
    total = len(groups)
    print(f"\n▶ [{label}] 스킵 그룹 수집 시작: {total}그룹")

    for gi, grp in enumerate(groups, 1):
        all_codes  = grp["all_codes"]
        targets    = grp["targets"]        # [(cd, nm), ...]
        gtype      = grp["group_type"]
        only_sex   = grp["only_sex"]       # "1"/"2"/None
        target_cds = {cd for cd, _ in targets}

        # 그룹 전체 코드의 가입금액: min_amt 지정 시 우선, payload 원본값, 없으면 1000천원
        min_amt    = grp.get("min_amt")
        group_amts = {
            cd: (min_amt if min_amt else payload_amounts.get(cd, 1000))
            for cd in all_codes
        }

        tag = ",".join(all_codes[:3]) + ("..." if len(all_codes) > 3 else "")
        print(f"  [{gi:>2}/{total}] {gtype} [{tag}]  대상담보:{len(targets)}개")

        for period in periods:
            period_nm = PERIOD_LABEL.get(period, period)
            for age in AGES:
                sex_list = [(s, n) for s, n in SEXES if only_sex is None or s == only_sex]
                for sex_cd, sex_nm in sex_list:
                    effective = dict(group_amts)
                    for attempt in range(5):
                        payload = make_payload(base_payload, BIRTH[age], sex_cd, period, effective)
                        try:
                            resp   = call_api(session, payload)
                            parsed = parse_response(resp, age, sex_nm, period_nm)
                            for item in parsed:
                                if item["담보코드"] in target_cds:
                                    item["상품"]     = label
                                    item["스킵사유"] = gtype
                                    rows.append(item)
                            break
                        except RuntimeError as e:
                            err_msg = str(e)
                            removed = 0
                            if "동시가입불가" in err_msg:
                                for m2 in re.finditer(
                                    r'([1-9][A-Z0-9]{3})([1-9][A-Z0-9]{3})\[동시가입불가\]', err_msg
                                ):
                                    cd_r = m2.group(2)
                                    if cd_r in effective and cd_r not in target_cds:
                                        del effective[cd_r]
                                        removed += 1
                            elif "ULT01009" in err_msg or (
                                "ULT00016" in err_msg and "키구성" in err_msg
                            ):
                                m_cd = re.search(r'담보:([1-9][A-Z0-9]{3})', err_msg)
                                candidates = (
                                    [m_cd.group(1)] if m_cd
                                    else re.findall(r'([1-9][A-Z0-9]{3})', err_msg)
                                )
                                for cd_r in candidates:
                                    if cd_r in effective and cd_r not in target_cds:
                                        del effective[cd_r]
                                        removed += 1
                                        break
                            if removed:
                                continue
                            print(f"    {period_nm} {age}세 {sex_nm}: {err_msg[:100]}")
                            break
                        except Exception as e:
                            if attempt < 2:
                                time.sleep(3)
                                continue
                            print(f"    {period_nm} {age}세 {sex_nm}: 실패 {e}")
                            break
                    time.sleep(0.3)

    print(f"  → 스킵 그룹 수집 완료: {len(rows)}행")
    return rows


def main():
    payloads = load_payloads()

    print("▶ 세션 초기화...")
    session = init_session()

    if TEST_MODE:
        label, periods, base_payload = payloads[0]
        period = periods[1] if len(periods) > 1 else periods[0]  # 두 번째 납입기간으로 테스트
        print(f"🧪 테스트: [{label}] {PERIOD_LABEL.get(period, period)} 50세 남 1회")
        resp = call_api(session, make_payload(base_payload, BIRTH[50], "1", period))
        print_test_result(resp)
        return

    all_rows   = []
    skip_rows  = []

    for label, periods, base_payload in payloads:
        req     = base_payload.get("request", base_payload)
        prod_cd = req.get("ltapcommonVO", {}).get("inagProdCd", "167D")

        extra_amounts = load_coverage_amounts(prod_cd)
        if extra_amounts:
            print(f"▶ [{label}] coverage_amounts 로드: {len(extra_amounts)}개 ({prod_cd})")

        rows = collect_one(session, label, periods, base_payload, extra_amounts)
        all_rows.extend(rows)

        # 스킵 그룹 수집
        groups = load_skipped_groups(prod_cd)
        if groups:
            payload_amts = extract_payload_amounts(base_payload)
            skip_rows.extend(
                collect_skipped_groups(session, label, periods, base_payload, groups, payload_amts)
            )

    if not all_rows:
        print("수집된 데이터 없음")
        return

    all_rows = supplement_renewal_coverages(all_rows)

    out  = Path(__file__).parent / f"hi_보험료_{TODAY}.xlsx"
    cols_main = ["상품", "나이", "성별", "납입기간", "담보코드", "담보명", "담보보험료", "기본보험료", "총납입보험료"]
    cols_skip = ["상품", "나이", "성별", "납입기간", "담보코드", "담보명", "스킵사유", "담보보험료", "기본보험료", "총납입보험료"]

    df_main = pd.DataFrame(all_rows)
    df_main = df_main[[c for c in cols_main if c in df_main.columns]]

    df_skip = pd.DataFrame(skip_rows) if skip_rows else pd.DataFrame(columns=cols_skip)
    if not df_skip.empty:
        df_skip = df_skip[[c for c in cols_skip if c in df_skip.columns]]

    def autofit(ws):
        for col in ws.columns:
            mx = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(mx + 4, 45)

    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df_main.to_excel(w, index=False, sheet_name="보험료")
        autofit(w.sheets["보험료"])
        df_skip.to_excel(w, index=False, sheet_name="스킵담보")
        autofit(w.sheets["스킵담보"])

    print(f"\n✅ {out.name} 저장  (보험료:{len(df_main):,}행 / 스킵담보:{len(df_skip):,}행)")
    print(df_main.groupby(["상품", "납입기간", "성별"])["담보보험료"].count().rename("담보수").to_string())
    if not df_skip.empty:
        print("\n[스킵담보 수집 현황]")
        print(df_skip.groupby(["상품", "스킵사유", "담보코드"])["담보보험료"].count().rename("행수").to_string())


if __name__ == "__main__":
    main()
