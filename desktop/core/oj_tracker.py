"""OISystem ZZOI 集成模块。

基于 zzoi 参考项目的 fetcher 思路，精简实现：
- 登录 ZZOI（Hydro OJ，登录表单字段为 uname）
- 抓取用户提交记录（/record?uidOrName=）
- 检测作业/比赛列表（优先解析页面内嵌 UiContextNew JSON，回退 HTML 表格/链接）
- 检测排行榜位置
- 当日零提交或排行榜末尾 → 触发 FocusEngine.lock_for_zzoi
- 检测到当日有提交 → 请求 force_release_if_locked 解除锁定
- 做题退出 v2：真实题目源（作业/比赛题目池）+ AC 检测

实测适配（Hydro 架构 OJ，2026-08-25）：
- 登录 POST 字段 uname/password/tfa/authnChallenge；成功后跳回首页并种 sid cookie
- 用户主页路径 /d/{domain}/user/{数字uid}（用户名不可用）
- 作业列表页 UiContextNew.docs(docType=30) 含 _id/title/pids/beginAt/endAt
- 比赛详情页 UiContextNew.tdoc.pids 为该比赛题目
- 提交列表对学生隐藏题目列（显示 *），但支持 pid={展示ID} 过滤；
  时间单元格带 data-timestamp（epoch 秒），日期格式不补零（2026-8-21）
"""
import json
import random
import re

import requests
from typing import List, Dict, Optional

from config.settings import ConfigManager
from utils.helpers import (
    logger, log_event, now_cst, format_time, parse_oj_time, normalize_status,
    CST
)


class ZzoiTracker:
    """ZZOI 提交记录与作业/比赛检测器。"""

    def __init__(self):
        self.cfg = ConfigManager()
        self._session = requests.Session()
        # 模拟浏览器 User-Agent，避免被 CloudFlare/WAF 拦截
        self._session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._logged_in = False
        self._last_check_time = ""
        # round48 P1：抓取结果有效性标志。网络错误/非 200 时置 False，
        # daily_check 依据此标志跳过"零提交/排行榜末尾"锁定，避免误锁。
        self._last_fetch_ok = True

    # ---------- 配置 ----------
    def _settings(self):
        s = self.cfg.settings
        return {
            "base_url": s.zzoi_base_url or "https://zzoi.com.cn",
            "domain_prefix": s.zzoi_domain_prefix or "d/ZZOI",
            "uid": s.zzoi_uid,
            "password": s.zzoi_password,
            "sid": s.zzoi_sid,
            "sid_sig": s.zzoi_sid_sig,
        }

    def _is_configured(self) -> bool:
        s = self._settings()
        return bool(s["uid"] and s["base_url"])

    # ---------- 登录 ----------
    def _reset_session(self):
        """重建 HTTP 会话（清除被 WAF 污染的 cookie/连接）。"""
        try:
            self._session.close()
        except Exception:
            pass
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._logged_in = False

    def login(self) -> bool:
        """登录 ZZOI。优先用 sid cookie，失败则用密码。"""
        if not self._is_configured():
            logger.warning("ZZOI 未配置")
            return False
        s = self._settings()
        try:
            # 用 sid cookie
            if s["sid"]:
                self._session.cookies.set("sid", s["sid"],
                                          domain=self._domain(s["base_url"]))
                if s["sid_sig"]:
                    self._session.cookies.set("sid.sig", s["sid_sig"],
                                              domain=self._domain(s["base_url"]))
                # 验证
                if self._check_login():
                    self._logged_in = True
                    logger.info("ZZOI 登录成功（sid cookie）")
                    return True

            # 用密码登录（Hydro 表单字段为 uname，附带 tfa/authnChallenge 隐藏域）
            if s["password"]:
                login_url = f"{s['base_url'].rstrip('/')}/login"
                resp = self._session.post(login_url, data={
                    "uname": s["uid"],
                    "password": s["password"],
                    "tfa": "",
                    "authnChallenge": "",
                    "rememberme": "on",
                    "login_submit": "",
                }, timeout=15, allow_redirects=True)
                if resp.status_code == 200 and self._check_login():
                    self._logged_in = True
                    logger.info("ZZOI 登录成功（密码）")
                    return True

            logger.warning("ZZOI 登录失败")
            return False
        except requests.RequestException as e:
            logger.warning(f"ZZOI 登录网络错误: {e}")
            # 网络失败时重建会话（WAF 可能污染了连接池）
            self._reset_session()
            return False

    def _domain(self, url: str) -> str:
        from urllib.parse import urlparse
        # r49 P2：无 scheme 的 URL（如 "zzoi.com.cn"）netloc 返回空，需补全
        if "://" not in url:
            url = "https://" + url
        return urlparse(url).netloc

    def _check_login(self) -> bool:
        """访问用户主页验证登录态。

        实测：/d/{domain}/user/{uid} 仅接受数字 uid（用户名 404）。
        配置里可能填的是用户名（登录表单用），因此先试原值，404 时
        再退回"访问域首页看是否仍被重定向到 /login"的判定方式。
        """
        s = self._settings()
        try:
            url = f"{s['base_url'].rstrip('/')}/{s['domain_prefix']}/user/{s['uid']}"
            resp = self._session.get(url, timeout=10)
            if resp.status_code == 200 and "login" not in resp.url.lower():
                return True
            if resp.status_code == 404:
                # 用户名登录场景：站点根路径未登录会被重定向到 /login?redirect=...
                home = self._session.get(
                    s["base_url"].rstrip("/") + "/", timeout=10)
                return "login" not in str(getattr(home, "url", "")).lower()
            return False
        except Exception:
            return False

    # ---------- 提交记录 ----------
    def _invalidate_on_fetch_error(self, e: Exception):
        """全量检修修复：抓取失败时区分网络错误与解析错误。

        网络错误/HTTP 非 200（会话可能被 WAF 污染或登录态失效）时重置会话与
        _logged_in，保证下次调用重新登录；否则一次网络抖动会让 _logged_in
        永远停在 True，后续抓取永久返回空结果。
        """
        if isinstance(e, requests.RequestException):
            logger.warning(f"ZZOI 抓取网络错误，重置会话: {e}")
            self._reset_session()

    # ---------- 页面数据载体解析 ----------
    _UICTX_RE = re.compile(r"var UiContextNew = '")

    @staticmethod
    def _parse_uicontext(html: str) -> Optional[dict]:
        """提取页面内嵌的 UiContextNew JSON（Hydro 列表/详情页的数据源）。

        该变量是单引号包裹的 JSON 字符串，且同 script 块内可能还有后续
        语句，用 raw_decode 只取第一个合法 JSON，避免贪婪匹配。
        """
        m = ZzoiTracker._UICTX_RE.search(html)
        if not m:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(html[m.end():])
            return data if isinstance(data, dict) else None
        except Exception:
            return False

    def _get_page(self, path: str, timeout: int = 15):
        """已登录 GET 域内页面。非 200 返回 None。"""
        s = self._settings()
        url = f"{s['base_url'].rstrip('/')}/{s['domain_prefix']}/{path.lstrip('/')}"
        return self._session.get(url, timeout=timeout)

    def fetch_today_submissions(self) -> List[Dict]:
        """抓取当日提交记录（/record?uidOrName=，实测适配 Hydro 架构 OJ）。

        DOM：col--status 单元格含 "100 Accepted" 等文本、题目列对学生为 *、
        时间单元格带 data-timestamp(epoch 秒)。学生号 pid 不可得（空串），
        pid 级检测走 check_problem_solved_today 的过滤通道。
        安全语义：任一页失败整体按抓取失败处理（fetch_ok=False），
        绝不让部分成功被误判为零提交而触发锁定；空页收口视为成功。
        """
        self._last_fetch_ok = False
        if not self._ensure_logged_in():
            return []
        s = self._settings()
        try:
            submissions = []
            today = now_cst().strftime("%Y-%m-%d")
            reached_empty_page = False
            for page in (1, 2, 3):
                resp = self._get_page(
                    f"record?uidOrName={s['uid']}&page={page}", timeout=15)
                if resp is None or resp.status_code != 200:
                    logger.warning(f"ZZOI record 请求失败: "
                                   f"{getattr(resp, 'status_code', 'n/a')}")
                    # 前面页可能已置 True，早退必须显式复位（防零提交误锁）
                    self._last_fetch_ok = False
                    self._reset_session()
                    return []
                rows = self._parse_record_rows(resp.text)
                if not rows:
                    reached_empty_page = True
                    break
                from datetime import datetime as _dt
                for row in rows:
                    try:
                        row_date = _dt.fromtimestamp(
                            int(row.get("ts") or 0), tz=CST).strftime("%Y-%m-%d")
                    except (ValueError, TypeError, OverflowError, OSError):
                        row_date = ""
                    if row_date == today:
                        submissions.append(row)
                self._last_fetch_ok = True
            logger.info(f"ZZOI 当日提交: {len(submissions)} 条 "
                        f"(fetch_ok={self._last_fetch_ok})")
            return submissions
        except Exception as e:
            logger.warning(f"ZZOI 抓取提交失败: {e}")
            self._last_fetch_ok = False
            self._invalidate_on_fetch_error(e)
            return []

    @staticmethod
    def _parse_record_rows(html: str) -> List[Dict]:
        """解析提交列表行：rid/status/score/ts/pid(学生号为空)。"""
        out: List[Dict] = []
        for r0 in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            rid_m = re.search(r"/record/([0-9a-fA-F]+)", r0)
            if not rid_m:
                continue
            stat_m = re.search(r'<td class="col--status[^"]*"[^>]*>(.*?)</td>', r0, re.S)
            status = score = ""
            if stat_m:
                txt = re.sub(r"\s+", " ",
                             re.sub(r"<[^>]+>", " ", stat_m.group(1))).strip()
                parts = txt.split()
                if parts:
                    score = parts[0]
                    status = normalize_status(txt)
            ts_m = re.search(r'data-timestamp="(\d+)"', r0)
            pid_m = re.search(r'href="/[^"]*?/(?:p|showProblem)/([^"]+)"', r0)
            date_m = re.search(r'(\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{2}:\d{2})', r0)
            out.append({
                "rid": rid_m.group(1),
                "pid": (pid_m.group(1) if pid_m else ""),
                "status": status,
                "score": score,
                "ts": int(ts_m.group(1)) if ts_m else 0,
                "time_str": date_m.group(1) if date_m else "",
            })
        return out

    # ---------- 作业/比赛检测 ----------
    def fetch_homework_list(self) -> List[Dict]:
        """抓取作业列表。

        实测：作业列表页无表格，数据在 UiContextNew.docs(docType=30)，
        含 _id/title/pids/beginAt/endAt/assign。返回兼容结构
        [{id, title, pids, begin_at, end_at, count}]；解析不出时回退旧表格。
        """
        self._last_fetch_ok = False
        if not self._ensure_logged_in():
            return []
        try:
            resp = self._get_page("homework", timeout=15)
            if resp is None or resp.status_code != 200:
                logger.warning(f"ZZOI homework 页请求失败: "
                               f"{getattr(resp, 'status_code', 'n/a')}")
                self._reset_session()
                return []
            ui = self._parse_uicontext(resp.text)
            hw_list: List[Dict] = []
            if isinstance(ui, dict):
                for d in ui.get("docs") or []:
                    if not isinstance(d, dict) or d.get("docType") != 30:
                        continue
                    pids = [str(p) for p in (d.get("pids") or [])
                            if str(p).strip()]
                    hw_list.append({
                        "id": str(d.get("_id") or d.get("docId") or ""),
                        "title": str(d.get("title") or "")[:80],
                        "pids": pids,
                        "count": len(pids),
                        "begin_at": str(d.get("beginAt") or ""),
                        "end_at": str(d.get("endAt") or ""),
                        "assign": ", ".join(d.get("assign") or [])
                        if isinstance(d.get("assign"), list) else "",
                    })
            if hw_list:
                self._last_fetch_ok = True
                logger.info(f"ZZOI 作业列表(UiContext): {len(hw_list)} 条")
                return hw_list
            # 回退：旧表格解析（其他 Hydro 站点可能仍是表格布局）
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            for row in soup.select("tr"):
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                hw_list.append({
                    "id": cells[0].get_text(strip=True),
                    "title": cells[1].get_text(strip=True),
                    "deadline": cells[2].get_text(strip=True),
                })
            self._last_fetch_ok = True
            logger.info(f"ZZOI 作业列表(table): {len(hw_list)} 条")
            return hw_list
        except Exception as e:
            logger.warning(f"ZZOI 作业列表抓取失败: {e}")
            self._invalidate_on_fetch_error(e)
            return []

    def fetch_contest_list(self) -> List[Dict]:
        """抓取比赛列表。

        实测：列表项为 <h1 class="contest__title"><a href=".../contest/{id}">
        标题</a> + supplementary 列表（状态/时间文本），无表格。
        回退旧表格解析以兼容其他 Hydro 站点。返回
        [{id, title, end_time, status_text}]。
        """
        self._last_fetch_ok = False
        if not self._ensure_logged_in():
            return []
        try:
            resp = self._get_page("contest", timeout=15)
            if resp is None or resp.status_code != 200:
                logger.warning(f"ZZOI contest 页请求失败: "
                               f"{getattr(resp, 'status_code', 'n/a')}")
                self._reset_session()
                return []
            html = resp.text
            c_list: List[Dict] = []
            # 通道1：media 分块（实测结构）
            blocks = re.split(r'<(?:div|li)[^>]*class="[^"]*media(?:__| )[^\"]*"',
                              html)
            for blk in blocks[1:]:
                m = re.search(r'href="[^"]*/contest/([A-Za-z0-9_\-]+)"', blk)
                if not m:
                    continue
                t = re.search(r'<h1[^>]*>\s*<a[^>]*>([^<]+)</a>', blk)
                sup = re.search(r'<ul class="supplementary[^>]*>(.*?)</ul>', blk, re.S)
                status_text = ""
                end_time = ""
                if sup:
                    status_text = re.sub(
                        r"\s+", " ",
                        re.sub(r"<[^>]+>", " ", sup.group(1))).strip()[:80]
                    d = re.search(r"(\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{2})", status_text)
                    if d:
                        end_time = d.group(1)
                c_list.append({
                    "id": m.group(1),
                    "title": (t.group(1).strip() if t else "")[:80],
                    "end_time": end_time,
                    "status_text": status_text,
                })
            if c_list:
                self._last_fetch_ok = True
                logger.info(f"ZZOI 比赛列表(media): {len(c_list)} 条")
                return c_list
            # 通道2：表格兜底
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for row in soup.select("tr"):
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                c_list.append({
                    "id": cells[0].get_text(strip=True),
                    "title": cells[1].get_text(strip=True),
                    "end_time": cells[2].get_text(strip=True),
                })
            self._last_fetch_ok = True
            return c_list
        except Exception as e:
            logger.warning(f"ZZOI 比赛列表抓取失败: {e}")
            self._invalidate_on_fetch_error(e)
            return []

    # ---------- 真实题目源（做题退出 v2） ----------
    _PID_RE = re.compile(r"^[A-Za-z]?[0-9A-Za-z_\-]{1,32}$")

    def _ensure_logged_in(self) -> bool:
        """统一登录入口：已登录直接 True，未登录尝试登录。"""
        if self._logged_in:
            return True
        return self.login()

    def _problem_url(self, pid: str) -> str:
        s = self._settings()
        return f"{s['base_url'].rstrip('/')}/{s['domain_prefix']}/p/{pid}"

    @staticmethod
    def _normalize_pid(raw: str) -> str:
        """pid 归一化：去空白与常见包裹符；非法字符直接返回空串。

        注意：正则只做字符白名单，纯英文词（如 "abc"）也会放行——
        这是刻意的宽松策略（比赛内题号可能是 "A"/"ch01" 等），
        垃圾输入由各来源通道自身的结构约束（链接 href/表格列语义）过滤。
        """
        if not isinstance(raw, str):
            return ""
        pid = raw.strip().strip("#").strip()
        # 形如 "P1234"、"T12"、"ch01"、"A" 均合法
        if not ZzoiTracker._PID_RE.match(pid):
            return ""
        return pid

    def fetch_contest_problems(self, contest_id: str) -> List[Dict]:
        """抓取指定比赛的题目列表。

        实测：Hydro 架构 OJ 比赛详情页 UiContextNew.tdoc.pids 即题目内部 id；
        通用兜底为页面题目链接解析。返回 [{pid, title}]。
        """
        self._last_fetch_ok = False
        if not contest_id or not self._ensure_logged_in():
            return []
        try:
            resp = self._get_page(f"contest/{str(contest_id).strip()}", timeout=15)
            if resp is None or resp.status_code != 200:
                logger.warning(f"ZZOI 比赛题目页请求失败: "
                               f"{getattr(resp, 'status_code', 'n/a')}")
                self._reset_session()
                return []

            problems: List[Dict] = []
            seen = set()

            def _add(pid_raw, title):
                pid = self._normalize_pid(pid_raw)
                if not pid or pid in seen:
                    return
                seen.add(pid)
                problems.append({"pid": pid,
                                 "title": (title or pid)[:80]})

            # 通道1（实测 Hydro 架构 OJ）：UiContextNew.tdoc.pids 为比赛题目内部 id
            ui = self._parse_uicontext(resp.text)
            if isinstance(ui, dict):
                tdoc = ui.get("tdoc") if isinstance(ui.get("tdoc"), dict) else {}
                for p in (tdoc.get("pids") or []):
                    _add(str(p), "")
                cdoc = ui.get("cdoc") if isinstance(ui.get("cdoc"), dict) else {}
                for p in (cdoc.get("pids") or []):
                    _add(str(p), "")

            # 通道2：页面内指向题目的链接（其他 Hydro 实例/权限可见时）
            if not problems:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.select("a[href]"):
                    href = str(a.get("href") or "")
                    m = re.search(r"/(?:p|showProblem)/([A-Za-z0-9_\-]+)", href)
                    if not m:
                        continue
                    _add(m.group(1), a.get_text(strip=True))

            self._last_fetch_ok = True
            logger.info(f"ZZOI 比赛 {contest_id} 题目数: {len(problems)}")
            return problems
        except Exception as e:
            logger.warning(f"ZZOI 比赛题目抓取失败: {e}")
            self._invalidate_on_fetch_error(e)
            return []

    def _fetch_problemset_page(self) -> List[Dict]:
        """兜底源：域内题库第一页。"""
        if not self._ensure_logged_in():
            return []
        s = self._settings()
        url = f"{s['base_url'].rstrip('/')}/{s['domain_prefix']}/p"
        try:
            resp = self._session.get(url, params={"page": 1}, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"ZZOI 题库页请求失败: {resp.status_code}")
                return []
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            problems: List[Dict] = []
            seen = set()
            for a in soup.select("a[href]"):
                href = str(a.get("href") or "")
                m = re.search(r"/(?:p|showProblem)/([A-Za-z0-9_\-]+)", href)
                if not m:
                    continue
                pid = self._normalize_pid(m.group(1))
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                problems.append({"pid": pid,
                                 "title": (a.get_text(strip=True) or pid)[:80]})
            return problems
        except Exception as e:
            logger.warning(f"ZZOI 题库页抓取失败: {e}")
            return []

    def fetch_problem_pool(self) -> Dict:
        """组合真实目标池（做题退出 v2），逐级降级。

        实测 Hydro 架构 OJ：学生号看不到题目页/记录里的 pid 列，但
        UiContextNew 提供作业（含 pids 数量与起止时间）与比赛 tdoc.pids。
        因此"目标"以作业/比赛为单位，而非单题；检测策略见 check_solved。

        降级链：进行中作业 → 进行中比赛 → 最近作业 → 题库页(通用兜底)
        → 已结束/时间未知比赛（最后手段）。每级最多尝试 5 个来源。

        返回 {"targets": [...], "source": str, "source_note": str}。
        """
        targets: List[Dict] = []
        source = ""
        source_note = ""

        def _strict_parse(raw: str):
            raw = (raw or "").strip()
            if not raw:
                return None
            from datetime import datetime as _dt
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                try:
                    from utils.helpers import CST
                    return _dt.strptime(raw, fmt).replace(tzinfo=CST)
                except ValueError:
                    continue
            try:
                dt = _dt.fromisoformat(raw)
                if dt.tzinfo is None:
                    from utils.helpers import CST
                    dt = dt.replace(tzinfo=CST)
                return dt
            except (ValueError, TypeError):
                return None

        def _hw_target(hw: Dict) -> Dict:
            url = (f"{self._settings()['base_url'].rstrip('/')}/"
                   f"{self._settings()['domain_prefix']}/homework/{hw.get('id')}")
            count = int(hw.get("count") or len(hw.get("pids") or []) or 0)
            return {
                "kind": "homework", "key": str(hw.get("id") or ""),
                "title": f"作业《{hw.get('title') or '未命名'}》",
                "count": count,
                "url": url, "source": "homework",
            }

        def _ct_target(c: Dict, pids_n: int) -> Dict:
            ended = bool(c.get("_ended"))
            url = (f"{self._settings()['base_url'].rstrip('/')}/"
                   f"{self._settings()['domain_prefix']}/contest/{c.get('id')}")
            return {
                "kind": "contest", "key": str(c.get("id") or ""),
                "title": f"比赛《{c.get('title') or c.get('id')}》",
                "count": pids_n, "url": url, "source": "contest",
                "source_note": "该比赛已结束，若无法提交请换一题" if ended else "",
            }

        now = now_cst()

        # 1) 进行中的作业（begin<=now<=end，按结束时间近的优先）
        hws = self.fetch_homework_list()
        ongoing_hw = []
        for hw in hws:
            b = _strict_parse(str(hw.get("begin_at") or ""))
            e = _strict_parse(str(hw.get("end_at") or ""))
            started = (b is None) or (b <= now)
            not_ended = (e is None) or (e >= now)
            hw["_has_window"] = bool(b or e)
            if started and not_ended and hw.get("id"):
                hw["_sort"] = (now - (e or now)).total_seconds()
                ongoing_hw.append(hw)
        ongoing_hw.sort(key=lambda x: x.get("_sort", 0))
        for hw in ongoing_hw[:5]:
            targets.append(_hw_target(hw))
        if targets:
            source = "homework"

        # 2) 进行中比赛
        if not targets:
            contests = self.fetch_contest_list()
            ongoing_ct, done_ct = [], []
            for c in contests:
                if not isinstance(c, dict) or not c.get("id"):
                    continue
                e = _strict_parse(str(c.get("end_time") or ""))
                if e is None:
                    c["_ended"] = False
                    c["_sort"] = 999
                    done_ct.append(c)
                elif e < now:
                    c["_ended"] = True
                    c["_sort"] = (now - e).total_seconds() / 86400.0
                    done_ct.append(c)
                else:
                    ongoing_ct.append(c)
            done_ct.sort(key=lambda x: x.get("_sort", 999))
            picked = 0
            for c in ongoing_ct[:5] + done_ct[:5]:
                if picked >= 3:
                    break
                probs = self.fetch_contest_problems(c.get("id"))
                if probs:
                    targets.append(_ct_target(c, len(probs)))
                    picked += 1
            if targets:
                source = "contest"

        # 3) 全部作业按时间倒序兜底（含未开始/已结束）
        if not targets:
            for hw in hws:
                if hw.get("id"):
                    targets.append(_hw_target(hw))
            if targets:
                source = "homework"
                source_note = "没有进行中的作业，已列出全部作业"

        # 4) 题库第一页（通用 Hydro 兜底）
        if not targets:
            legacy = self._fetch_problemset_page()
            for p in legacy[:20]:
                targets.append({
                    "kind": "problem", "key": str(p.get("pid")),
                    "pid": str(p.get("pid")),
                    "title": f"{p.get('pid')} {p.get('title', '')}".strip(),
                    "url": self._problem_url(str(p.get("pid"))),
                    "count": 1, "source": "problemset",
                })
            if targets:
                source = "problemset"

        result = {"targets": targets, "source": source, "source_note": source_note}
        if targets:
            logger.info(f"ZZOI 目标池就绪: source={source}, n={len(targets)}")
        else:
            logger.warning("ZZOI 目标池为空（所有来源均失败）")
        return result

    def pick_problem(self, exclude_solved: bool = True) -> Optional[Dict]:
        """从真实目标池挑一个目标（作业/比赛/题库题）。

        返回 {"kind","key","title","url","count","source",
              "contest_title"(兼容),"source_note"} 或 None。
        """
        pool_data = self.fetch_problem_pool()
        targets = [t for t in pool_data.get("targets", [])
                   if isinstance(t, dict) and (t.get("key") or t.get("pid"))]
        if not targets:
            return None
        chosen = random.choice(targets)
        chosen.setdefault("contest_title", "")
        chosen.setdefault("source_note", pool_data.get("source_note", ""))
        return chosen

    def fetch_ac_pids_today(self) -> List[str]:
        """当日 AC 的展示 pid 集合（学生号下多为空，保留兼容）。"""
        ac = []
        for sub in self.fetch_today_submissions():
            try:
                if sub.get("status") == "AC" and sub.get("pid"):
                    ac.append(str(sub["pid"]).strip())
            except Exception:
                continue
        return ac

    def check_solved(self, display_pid: str = "", since_ts: int = 0) -> Dict:
        """做题退出完成判定（实测适配学生号权限）。

        - display_pid 非空：用 /record?uidOrName=&pid={display_pid} 过滤，
          存在 AC 且提交时间 >= since_ts（或当日）即完成——精确通道；
        - display_pid 为空：当日记录流里存在 ts >= since_ts 的 Accepted
          即完成——增量通道（分配后新 AC 视为完成）。

        返回 {"solved": bool, "fetch_ok": bool, "mode": "pid"/"new_ac"}。
        """
        s = self._settings()
        if not self._ensure_logged_in():
            return {"solved": False, "fetch_ok": False, "mode": ""}
        try:
            today = now_cst().strftime("%Y-%m-%d")
            if display_pid:
                # 展示ID 来自用户输入框：白名单清洗后再拼查询串，
                # 防止 "&page=2"/"#x" 之类字符改变请求语义
                clean_pid = self._normalize_pid(display_pid)
                if not clean_pid:
                    return {"solved": False, "fetch_ok": True, "mode": "pid"}
                path = (f"record?uidOrName={s['uid']}&pid={clean_pid}")
            else:
                path = f"record?uidOrName={s['uid']}&status=1"
            resp = self._get_page(path, timeout=15)
            if resp is None or resp.status_code != 200:
                logger.warning(f"ZZOI 记录过滤请求失败: "
                               f"{getattr(resp, 'status_code', 'n/a')}")
                self._reset_session()
                return {"solved": False, "fetch_ok": False, "mode":
                        "pid" if display_pid else "new_ac"}
            rows = self._parse_record_rows(resp.text)
            mode = "pid" if display_pid else "new_ac"
            for row in rows:
                if row.get("status") != "AC":
                    continue
                ts = int(row.get("ts") or 0)
                try:
                    from datetime import datetime as _dt
                    d = _dt.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%d")
                except (ValueError, TypeError, OverflowError, OSError):
                    d = ""
                if display_pid:
                    # 有分配时刻则要求"分配之后"，否则只认当日
                    ok_time = (ts >= since_ts) if since_ts else (d == today)
                else:
                    ok_time = ts >= (since_ts or 0)
                if ok_time:
                    return {"solved": True, "fetch_ok": True, "mode": mode,
                            "rid": row.get("rid", ""), "ts": ts}
            return {"solved": False, "fetch_ok": True, "mode": mode}
        except Exception as e:
            logger.warning(f"ZZOI 完成判定失败: {e}")
            self._invalidate_on_fetch_error(e)
            return {"solved": False, "fetch_ok": False,
                    "mode": "pid" if display_pid else "new_ac"}

    def check_problem_solved_today(self, pid: str) -> bool:
        """检查指定展示 pid 当日是否已有 AC 提交（保留旧接口兼容）。"""
        pid = self._normalize_pid(pid)
        if not pid:
            return False
        return bool(self.check_solved(display_pid=pid).get("solved"))

    def check_new_ac_since(self, since_ts: int) -> bool:
        """检查指定时刻之后是否出现新的 Accepted 提交。"""
        return bool(self.check_solved(since_ts=since_ts).get("solved"))

    # ---------- 排行榜 ----------
    def check_rank_tail(self) -> bool:
        """检查用户是否在排行榜末尾。

        全量检修修复：原为永久返回 False 的 stub，导致 focus_lock_on_rank_tail
        配置完全失效。现实现保守版解析：拉取排行榜首页，若找到用户排名且
        位于可见榜单末尾区间，才判定为末尾。任何异常/解析失败一律返回 False
        （宁可漏报不误锁）。
        """
        if not self._is_configured():
            return False
        if not self._logged_in:
            if not self.login():
                return False
        s = self._settings()
        try:
            url = f"{s['base_url'].rstrip('/')}/{s['domain_prefix']}/ranking"
            resp = self._session.get(url, timeout=15)
            if resp.status_code != 200:
                self._reset_session()
                return False
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select("tr")
            uid_str = str(s["uid"])
            my_rank = None
            total_rows = 0
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                total_rows += 1
                row_text = row.get_text(" ", strip=True)
                if uid_str in row_text and my_rank is None:
                    # 首列通常是名次
                    rank_txt = cells[0].get_text(strip=True)
                    try:
                        my_rank = int(rank_txt)
                    except ValueError:
                        my_rank = total_rows
            if my_rank is None or total_rows <= 0:
                return False
            # 保守判定：名次位于本页可见榜单的最后 10%（且至少倒数 3 名内）
            tail_threshold = max(total_rows - 3, int(total_rows * 0.9))
            return my_rank >= tail_threshold
        except Exception as e:
            logger.warning(f"ZZOI 排行榜检查失败: {e}")
            self._invalidate_on_fetch_error(e)
            return False

    # ---------- 每日检查 ----------
    def daily_check(self, focus_engine=None) -> dict:
        """每日检查：零提交/排行榜末尾 → 触发 FocusEngine 锁定。

        返回 {has_submission, rank_tail, locked, login_ok}
        网络错误不触发锁定（避免误判零提交）。
        """
        result = {"has_submission": False, "rank_tail": False, "locked": False}

        if not self._is_configured():
            logger.info("ZZOI 未配置，跳过每日检查")
            return result

        # 先尝试登录，登录失败说明网络问题不触发锁定
        if not self._logged_in:
            login_ok = self.login()
            result["login_ok"] = login_ok
            if not login_ok:
                logger.warning("ZZOI 登录失败（网络错误），跳过零提交检测，不触发锁定")
                log_event("zzoi_skip_check", {"reason": "login_failed"})
                return result

        submissions = self.fetch_today_submissions()
        result["has_submission"] = len(submissions) > 0
        result["fetch_ok"] = self._last_fetch_ok

        # round48 P1：网络错误/非 200 时抓取结果不可信，禁止触发零提交锁定，
        # 否则一次网络抖动会把用户误锁进 365 天专注（宁可漏报不误锁）。
        if not self._last_fetch_ok:
            logger.warning("ZZOI 提交抓取失败，跳过零提交/排行榜锁定（避免误锁）")
            log_event("zzoi_skip_check", {"reason": "fetch_failed"})
            self._last_check_time = format_time(now_cst())
            log_event("zzoi_daily_check", result)
            return result

        # 记录提交错误
        for sub in submissions:
            status = sub.get("status", "")
            if status not in ("AC",):
                log_event("submission_error", {
                    "pid": sub.get("pid"),
                    "type": status,
                })

        # 子AGENT终审 C-2：当日已有提交时，若处于 ZZOI 锁定态则请求解除。
        # focus_engine 可能是主线程桥/记录器，force_release_if_locked 缺失时静默跳过
        if result["has_submission"]:
            result["release_requested"] = True
            rel = getattr(focus_engine, "force_release_if_locked", None)
            if callable(rel):
                try:
                    rel()
                    result["released"] = True
                except Exception as e:
                    logger.warning(f"ZZOI 锁定解除请求失败: {e}")

        # 零提交检查
        s = self.cfg.settings
        if s.focus_lock_on_no_submission and not result["has_submission"]:
            logger.warning("检测到当日 ZZOI 零提交，触发专注锁定")
            log_event("zzoi_no_submission_detected", {})
            if focus_engine:
                focus_engine.lock_for_zzoi("zzoi_no_submit")
                result["locked"] = True

        # 排行榜末尾检查
        if s.focus_lock_on_rank_tail and self.check_rank_tail():
            result["rank_tail"] = True
            logger.warning("检测到排行榜末尾，触发专注锁定")
            log_event("zzoi_rank_tail_detected", {})
            if focus_engine:
                focus_engine.lock_for_zzoi("zzoi_rank_tail")
                result["locked"] = True

        self._last_check_time = format_time(now_cst())
        log_event("zzoi_daily_check", result)
        return result
