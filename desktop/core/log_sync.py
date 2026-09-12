"""OISystem 日志同步系统。

职责：
- note.ms 同步：GET 取现有内容，POST 追加新内容到 note.ms/YYYYMMDDOISYSTEM
- 邮件发送：复用 zzoi mailer 思路，smtplib + email.mime
- 极域快照打包：扫描 Snapshots 目录，zipfile 打包，附件发送
"""
import os
import smtplib
import zipfile
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Optional

from config.settings import ConfigManager
from utils.helpers import (
    logger, log_event, today_str, now_cst, format_time,
    get_today_log, DATA_DIR
)


# ============ note.ms 同步 ============

# r41 修复：note.ms 已启用 Cloudflare 人机验证，裸请求会被 403 拦截。
# 统一添加浏览器 User-Agent，并在返回 Cloudflare 挑战页时给出明确日志。
_NOTE_MS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _note_ms_base_url() -> str:
    """返回 note.ms 基础 URL（round48：消费 note_ms_base_url，不再是死配置）。"""
    try:
        base = ConfigManager().settings.note_ms_base_url or "https://note.ms"
    except Exception:
        base = "https://note.ms"
    return str(base).rstrip("/")


def _note_ms_url() -> str:
    """返回当日 note.ms URL，如 https://note.ms/20260720OISYSTEM"""
    return f"{_note_ms_base_url()}/{today_str()}{ConfigManager().settings.note_ms_suffix}"


def _note_ms_slug() -> str:
    """返回 URL slug 部分，用于 API 调用。"""
    s = ConfigManager().settings
    return f"{today_str()}{s.note_ms_suffix}"


def _note_ms_api_url(slug: str) -> str:
    """由用户配置的 base URL 推导 API 端点，而不是硬编码 https://note.ms。"""
    return f"{_note_ms_base_url()}/api/notes/{slug}"


def _is_cloudflare_challenge(resp) -> bool:
    """判断响应是否为 Cloudflare 人机验证页。"""
    if resp.status_code != 403:
        return False
    text = resp.text.lower()
    return (
        "just a moment" in text
        or "cloudflare" in text
        or "turnstile" in text
        or "cf-im-under-attack" in text
    )


def fetch_note_ms() -> str:
    """获取 note.ms 当日内容。"""
    if not ConfigManager().settings.note_ms_enabled:
        return ""
    try:
        slug = _note_ms_slug()
        # note.ms API: GET /api/notes/{slug}/content
        url = f"{_note_ms_api_url(slug)}/content"
        resp = requests.get(url, headers=_NOTE_MS_HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.text
        if _is_cloudflare_challenge(resp):
            logger.warning(
                f"note.ms GET 被 Cloudflare 拦截（403 Just a moment...），"
                f"建议关闭 note.ms 同步或在浏览器中手动访问 {_note_ms_url()}"
            )
        else:
            logger.warning(f"note.ms GET 失败: {resp.status_code}")
        return ""
    except Exception as e:
        logger.warning(f"note.ms 获取失败: {e}")
        return ""


def append_to_note_ms(content: str) -> bool:
    """追加内容到 note.ms 当日笔记末尾。

    规则：若已有内容，追加到末尾；否则新建。
    """
    if not ConfigManager().settings.note_ms_enabled:
        return False
    if not content.strip():
        return False
    try:
        slug = _note_ms_slug()
        # 先获取现有内容
        existing = fetch_note_ms()
        # 追加
        if existing:
            new_content = existing.rstrip() + "\n\n" + content
        else:
            new_content = content
        # note.ms API: PUT /api/notes/{slug}
        url = _note_ms_api_url(slug)
        resp = requests.put(url, data=new_content.encode("utf-8"),
                            headers=_NOTE_MS_HEADERS, timeout=15)
        if resp.status_code in (200, 201):
            logger.info(f"note.ms 同步成功: {slug}")
            return True
        if _is_cloudflare_challenge(resp):
            logger.warning(
                f"note.ms PUT 被 Cloudflare 拦截（403 Just a moment...），"
                f"建议关闭 note.ms 同步或在浏览器中手动访问 {_note_ms_url()}"
            )
        else:
            logger.warning(f"note.ms PUT 失败: {resp.status_code}")
        return False
    except Exception as e:
        logger.warning(f"note.ms 同步失败: {e}")
        return False


def sync_today_log_to_note_ms() -> bool:
    """把当日结构化日志同步到 note.ms。"""
    data = get_today_log()
    lines = [f"# OISystem 日志 - {data.get('date', today_str())}", ""]

    lines.append("## 概览")
    lines.append(f"- 事件总数: {len(data.get('events', []))}")
    lines.append(f"- 专注会话数: {len(data.get('focus_sessions', []))}")
    lines.append(f"- AI 指出错误数: {len(data.get('ai_errors_found', []))}")
    lines.append(f"- 对话摘要数: {len(data.get('dialog_summaries', []))}")
    lines.append(f"- 危险操作数: {len(data.get('danger_ops', []))}")
    lines.append(f"- 提醒次数: {data.get('reminders', 0)}")
    lines.append(f"- 网站拦截数: {len(data.get('site_blocks', []))}")
    lines.append("")

    lines.append("## 提交错误统计")
    for et, n in data.get("submission_errors", {}).items():
        lines.append(f"- {et}: {n}")
    lines.append("")

    lines.append("## 事件流")
    for ev in data.get("events", []):
        lines.append(f"- [{ev.get('ts')}] {ev.get('type')}: {ev.get('detail')}")
    lines.append("")

    lines.append("## AI 指出的错误")
    for e in data.get("ai_errors_found", []):
        lines.append(f"- {e}")
    lines.append("")

    lines.append("## 对话摘要")
    for d in data.get("dialog_summaries", []):
        lines.append(f"- {d}")
    lines.append("")

    return append_to_note_ms("\n".join(lines))


# ============ 邮件发送 ============

def send_mail(subject: str, body: str, attachments: Optional[List[str]] = None) -> bool:
    """发送邮件。"""
    s = ConfigManager().settings
    if not s.mail_sender or not s.mail_receivers:
        logger.warning("邮件未配置收发件人")
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = s.mail_sender
        msg["To"] = ", ".join(s.mail_receivers)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        for fp in (attachments or []):
            if not os.path.isfile(fp):
                continue
            with open(fp, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{os.path.basename(fp)}"',
                )
                msg.attach(part)

        with smtplib.SMTP_SSL(s.mail_smtp_server, s.mail_smtp_port, timeout=30) as srv:
            srv.login(s.mail_sender, s.mail_password)
            srv.sendmail(s.mail_sender, s.mail_receivers, msg.as_string())
        logger.info(f"邮件已发送: {subject}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def send_daily_log_via_mail() -> bool:
    """把当日日志通过邮件发送。"""
    data = get_today_log()
    lines = [f"OISystem 日志 - {data.get('date', today_str())}", "=" * 40, ""]
    lines.append(f"事件总数: {len(data.get('events', []))}")
    lines.append(f"提醒次数: {data.get('reminders', 0)}")
    lines.append(f"AI 指出错误数: {len(data.get('ai_errors_found', []))}")
    lines.append("")
    for ev in data.get("events", []):
        lines.append(f"[{ev.get('ts')}] {ev.get('type')}: {ev.get('detail')}")
    return send_mail(
        subject=f"OISystem 日志 - {today_str()}",
        body="\n".join(lines),
    )


# ============ 极域快照打包 ============

def pack_and_send_snapshots() -> bool:
    """扫描极域 Snapshots 目录，打包图片，邮件发送。"""
    s = ConfigManager().settings
    if not s.jiyu_snapshot_send:
        return False
    snap_dir = s.jiyu_snapshot_path
    if not os.path.isdir(snap_dir):
        logger.warning(f"极域快照目录不存在: {snap_dir}")
        return False

    # 收集图片
    images = []
    for fname in os.listdir(snap_dir):
        if fname.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
            images.append(os.path.join(snap_dir, fname))
    if not images:
        logger.info("极域快照目录无图片")
        return False
    # round48：极端情况（快照目录被脚本灌入数万文件）设上限，避免打包内存/时间失控
    max_images = 500
    if len(images) > max_images:
        logger.warning(
            f"极域快照目录图片过多（{len(images)} 张），仅打包最近 {max_images} 张"
        )
        images = images[-max_images:]

    # 打包
    zip_path = os.path.join(DATA_DIR, f"snapshots_{today_str()}.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for img in images:
                zf.write(img, os.path.basename(img))
        logger.info(f"极域快照已打包: {zip_path} ({len(images)} 张)")
    except Exception as e:
        logger.error(f"极域快照打包失败: {e}")
        return False

    # 发送
    ok = send_mail(
        subject=f"OISystem 极域快照 - {today_str()}",
        body=f"附件为极域课堂管理系统 Snapshots 目录打包（{len(images)} 张图片）。",
        attachments=[zip_path],
    )
    if ok:
        log_event("snapshots_sent", {"count": len(images), "zip": zip_path})
    return ok


# ============ 一键同步 ============

def sync_all():
    """一键执行所有日志同步：note.ms + 邮件 + 极域快照。"""
    logger.info("开始全量日志同步")
    sync_today_log_to_note_ms()
    send_daily_log_via_mail()
    pack_and_send_snapshots()
    log_event("log_sync_all", {"ts": format_time(now_cst())})
    logger.info("全量日志同步完成")
