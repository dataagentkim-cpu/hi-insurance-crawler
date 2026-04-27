"""
현대해상 가격공시실 보험료 수집기
https://www.hi.co.kr/serviceAction.do

[사용법]
1. https://www.hi.co.kr/serviceAction.do 접속
2. F12 → Network 탭 → 보험 조건 설정 후 보험료 산출 클릭
3. 'ajax.xhi' 요청 클릭 → Payload 탭 → Copy 해서 captured_payload.json 저장
4. python3 hi_collect.py            # 전체 수집
   python3 hi_collect.py --test     # 1회 테스트 (캡처된 조건 그대로)
"""

import copy, json, re, sys, time
from datetime import datetime
from pathlib import Path
import requests
import pandas as pd

TEST_MODE = "--test" in sys.argv

TODAY    = datetime.today().strftime("%Y%m%d")
BASE     = "https://www.hi.co.kr"
API_URL  = f"{BASE}/ajax.xhi"

# 상품별 payload 파일 목록 (파일명: 표시명)
PAYLOAD_FILES = {
    "hi_payload_연만기_1종.json": "연만기갱신형 1종(표준형)",
    "hi_payload_연만기_2종.json": "연만기갱신형 2종(해약환급금미지급형)",
    "hi_payload_세만기_1종.json": "세만기형 1종(표준형)",
    "hi_payload_세만기_2종.json": "세만기형 2종(해약환급금미지급형)",
}

# 보험나이 기준 생년월일 (연도 - 보험나이 = 출생연도)
BIRTH = {
    30: "19960701", 35: "19910701", 40: "19860701",
    45: "19810701", 50: "19760701", 55: "19710701",
    60: "19660701", 65: "19610701", 70: "19560701",
}
AGES  = [30] if TEST_MODE else [30, 35, 40, 45, 50, 55, 60, 65, 70]
SEXES = [("2", "여")] if TEST_MODE else [("1", "남"), ("2", "여")]

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
      const blob = new Blob([body], {type:'application/json'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'hi_payload_연만기_1종.json';  // ← 저장할 파일명 변경
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      console.log('✅ 저장됨');
    }
    return _send.apply(this, arguments);
  };
  console.log('✅ 인터셉터 설치 완료 → 보험료 조회 클릭');
})();
"""


def load_payloads() -> list[tuple[str, dict]]:
    """존재하는 payload 파일만 로드. (파일명, 표시명, payload) 리스트 반환."""
    base_dir = Path(__file__).parent
    found = []
    missing = []

    for fname, label in PAYLOAD_FILES.items():
        fpath = base_dir / fname
        if fpath.exists():
            found.append((label, json.loads(fpath.read_text(encoding="utf-8"))))
        else:
            missing.append(fname)

    if missing:
        print(f"⚠️  아직 캡처 안 된 파일: {', '.join(missing)}")

    if not found:
        print("❌ payload 파일이 하나도 없습니다.")
        print(CAPTURE_GUIDE)
        print("\n상품별 저장 파일명:")
        for fname, label in PAYLOAD_FILES.items():
            print(f"  {label:30s} → {fname}")
        sys.exit(1)

    return found


def make_payload(base: dict, birth: str, sex_cd: str) -> dict:
    """베이스 payload의 나이·성별만 교체해서 반환."""
    p = copy.deepcopy(base)

    # ptyRegNo: 주민번호 앞 7자리 (생년월일 뒤 6자리 + 성별 코드)
    reg_no = birth[2:] + sex_cd + "000000"

    def patch_item(item: dict):
        if "ptyBrdt"   in item: item["ptyBrdt"]   = birth
        if "ptySxdsCd" in item: item["ptySxdsCd"] = sex_cd
        if "ptyRegNo"  in item: item["ptyRegNo"]  = reg_no

    def patch_any(obj):
        """dict이든 list든 재귀적으로 패치."""
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

    # ltapcommonVO 내 계약자/피보험자 주민번호 + 날짜
    lvo = req.get("ltapcommonVO", {})
    for fld in ("ctrtrPtyRegNo", "insrdPtyRegNo"):
        if fld in lvo:
            lvo[fld] = reg_no
    for fld in ("inagInsStDt", "savePremStDt", "premCalDt"):
        if fld in lvo:
            lvo[fld] = TODAY

    return p


def init_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": HEADERS["User-Agent"]})
    s.get(f"{BASE}/serviceAction.do", timeout=15)
    return s


def call_api(session: requests.Session, payload: dict) -> dict:
    r = session.post(API_URL, json=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def extract_elag_list(data: dict) -> list[dict]:
    """응답에서 담보 목록 추출 (보험료 > 0인 항목만)."""
    outer = data.get("elagInfoList", [])
    if not outer:
        return []
    # data.elagInfoList[0].elagInfoList 에 실제 담보 목록 있음
    inner = outer[0].get("elagInfoList", []) if isinstance(outer[0], dict) else []
    return [e for e in inner if float(e.get("tpprPrem", 0) or 0) > 0]


def parse_response(resp: dict, age: int, sex_nm: str) -> list[dict]:
    rs = resp.get("responseStatus", {})
    if rs.get("exceptionOccurred"):
        exc = rs.get("exception", {})
        raise RuntimeError(f"{exc.get('code')} {exc.get('message','')}")

    data       = resp.get("data", resp)
    common     = data.get("ltapcommonVO", {})
    elag_list  = extract_elag_list(data)
    total_prem = float(common.get("insrdPrem", 0) or 0)

    rows = []
    for e in elag_list:
        rows.append({
            "나이":       age,
            "성별":       sex_nm,
            "담보코드":    e.get("elagClsCd", ""),
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
    print(f"  보증보험료:   {float(common.get('guarPrem', 0) or 0):>12,.0f}원")
    print(f"\n  {'담보코드':<8} {'담보명':<35} {'보험료':>10}")
    print(f"  {'-'*55}")
    for e in elag_list:
        cd  = e.get("elagClsCd", "")
        nm  = e.get("elagElpaNm", cd)
        prm = float(e.get("tpprPrem", 0) or 0)
        print(f"  {cd:<8} {nm:<35} {prm:>10,.0f}원")
    print(f"{'='*60}\n")


def collect_one(session, label, base_payload) -> list[dict]:
    """하나의 상품에 대해 전체 나이/성별 수집."""
    total = len(AGES) * len(SEXES)
    rows  = []
    n     = 0
    print(f"\n▶ [{label}] {len(AGES)}나이 × {len(SEXES)}성별 = {total}회")

    for age in AGES:
        for sex_cd, sex_nm in SEXES:
            n += 1
            payload = make_payload(base_payload, BIRTH[age], sex_cd)
            for attempt in range(3):
                try:
                    resp = call_api(session, payload)
                    r    = parse_response(resp, age, sex_nm)
                    # 상품명 컬럼 추가
                    for item in r:
                        item["상품"] = label
                    rows.extend(r)
                    total_prem = r[0]["총납입보험료"] if r else 0
                    print(f"  [{n:>2}/{total}] {age}세 {sex_nm}  "
                          f"총납입:{int(total_prem):,}원  담보:{len(r)}개")
                    break
                except RuntimeError as e:
                    print(f"  [{n:>2}/{total}] {age}세 {sex_nm}: 오류 → {e}")
                    break
                except Exception as e:
                    if attempt < 2:
                        print(f"  [{n:>2}/{total}] {age}세 {sex_nm}: 재시도 {e}")
                        time.sleep(3)
                    else:
                        print(f"  [{n:>2}/{total}] {age}세 {sex_nm}: 실패 {e}")
            time.sleep(0.5)
    return rows


def main():
    payloads = load_payloads()

    print("▶ 세션 초기화...")
    session = init_session()

    if TEST_MODE:
        label, base_payload = payloads[0]
        print(f"🧪 테스트: [{label}] 30세 여 1회")
        resp = call_api(session, make_payload(base_payload, BIRTH[30], "2"))
        print_test_result(resp)
        return

    all_rows = []
    for label, base_payload in payloads:
        rows = collect_one(session, label, base_payload)
        all_rows.extend(rows)

    if not all_rows:
        print("수집된 데이터 없음")
        return

    df  = pd.DataFrame(all_rows)
    cols = ["상품", "나이", "성별", "담보코드", "담보명", "담보보험료", "기본보험료", "총납입보험료"]
    df  = df[[c for c in cols if c in df.columns]]
    out = Path(__file__).parent / f"hi_보험료_{TODAY}.xlsx"

    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="보험료")
        ws = w.sheets["보험료"]
        for col in ws.columns:
            mx = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(mx + 4, 45)

    print(f"\n✅ {out.name} 저장  ({len(df):,}행)")
    print(df.groupby(["상품", "성별"])["담보보험료"].count().rename("담보수").to_string())


if __name__ == "__main__":
    main()
