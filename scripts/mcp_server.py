import argparse
import base64
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

try:
    from mcp import types
    from mcp.server.fastmcp import FastMCP
except ImportError:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "mcp>=1.2.0"]
    )
    importlib.invalidate_caches()
    from mcp import types
    from mcp.server.fastmcp import FastMCP

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import heatmap_stats  # noqa: E402

UI_HTML_PATH = os.path.join(PLUGIN_ROOT, "ui", "app.html")
_UI_REV = int(os.path.getmtime(UI_HTML_PATH)) if os.path.exists(UI_HTML_PATH) else 1
APP_URI = f"ui://token-heatmap/app-v{_UI_REV}.html"

PANE_META = {
    "ui": {"resourceUri": APP_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "Token 热力图"},
            {"type": "global", "title": "Token 热力图"},
        ]
    },
}
APP_ONLY_META = {"ui": {"visibility": ["app"]}}

DATA_DIR = heatmap_stats.get_data_dir()
mcp = FastMCP("token-heatmap")


def _fmt_summary(data: dict) -> str:
    s = data.get("summary") or {}
    t = data.get("tools") or {}
    lev = data.get("leverage") or {}
    peak = data.get("peakDay") or {}
    chrono = data.get("chronotype") or {}
    peak_txt = f"{peak.get('date')} ({peak.get('fresh', 0):,} 新消耗)" if peak else "暂无"
    return (
        f"Token 战绩墙：累计活跃 {s.get('activeDays', 0)} 天（当前连击 {s.get('currentStreak', 0)} 天 / 最长 {s.get('longestStreak', 0)} 天），"
        f"共 {s.get('totalCalls', 0):,} 次交互；生成输出 {s.get('totalOutput', 0):,} Tokens，"
        f"总吞吐 {s.get('totalProcessed', 0):,} Tokens（缓存命中 {s.get('cacheHitRate', 0)}% · 源码写入 {t.get('writes', 0):,} 次 · 编辑命中 {lev.get('editHitRate', 100)}%）。"
        f"最肝一天：{peak_txt}；作息类型：{chrono.get('title', '')}。"
    )


@mcp.resource(
    APP_URI,
    name="Token 热力图与战绩墙",
    description="GitHub 风格年度 Token 贡献热力图、连续打卡成就、24 小时作息指纹与可导出纪念海报。",
    mime_type="text/html;profile=mcp-app",
    meta={"ui": {"prefersBorder": False}},
)
def heatmap_app_html() -> str:
    with open(UI_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


@mcp.tool(
    name="open_token_heatmap",
    title="打开 Token 热力图与战绩墙",
    description="展示 GitHub 风格历史 Token 贡献热力图、连续打卡成就、24 小时作息指纹及纪念战报导出。",
    meta=PANE_META,
)
def open_token_heatmap() -> types.CallToolResult:
    data = heatmap_stats.collect_heatmap_data(DATA_DIR)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_fmt_summary(data))],
        structuredContent=data,
    )


@mcp.tool(
    name="get_heatmap_data",
    title="获取热力图最新统计数据",
    description="供前端刷新热力图与里程碑统计。",
    meta=APP_ONLY_META,
)
def get_heatmap_data() -> types.CallToolResult:
    data = heatmap_stats.collect_heatmap_data(DATA_DIR)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_fmt_summary(data))],
        structuredContent=data,
    )


@mcp.tool(
    name="save_ui_prefs",
    title="保存热力图视觉与主题偏好",
    description="持久化前端滑块颗粒度与夜间/日间主题偏好，跨会话记忆。",
    meta=APP_ONLY_META,
)
def save_ui_prefs(granularity: int = None, theme_mode: str = None) -> types.CallToolResult:
    prefs = heatmap_stats.save_ui_prefs(DATA_DIR, granularity=granularity, theme_mode=theme_mode)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"已保存偏好：granularity={prefs['granularity']} theme={prefs['themeMode']}")],
        structuredContent=prefs,
    )


@mcp.tool(
    name="save_poster_png",
    title="保存纪念海报 PNG 到系统图片文件夹",
    description="将前端绘制的高清战绩纪念海报保存至用户图片文件夹。",
    meta=APP_ONLY_META,
)
def save_poster_png(data_url: str) -> types.CallToolResult:
    prefix = "data:image/png;base64,"
    if not data_url or not data_url.startswith(prefix):
        return types.CallToolResult(
            isError=True,
            content=[types.TextContent(type="text", text="无效的 PNG 图片数据")],
        )
    b64_part = data_url[len(prefix) :]
    if len(b64_part) > 10 * 1024 * 1024:
        return types.CallToolResult(
            isError=True,
            content=[types.TextContent(type="text", text="图片体积超出限制")],
        )
    raw = base64.b64decode(b64_part)
    pics_dir = os.path.join(os.path.expanduser("~"), "Pictures", "Antigravity-Heatmaps")
    os.makedirs(pics_dir, exist_ok=True)
    fname = f"antigravity-heatmap-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    out_path = os.path.join(pics_dir, fname)
    with open(out_path, "wb") as f:
        f.write(raw)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"已保存到：{out_path}")],
        structuredContent={"savedPath": out_path, "bytes": len(raw)},
    )


@mcp.tool(
    name="copy_poster_clipboard",
    title="复制纪念海报 PNG 到系统剪贴板",
    description="将前端绘制的高清战绩纪念海报直接写入系统剪贴板，支持微信/QQ/飞书等直接粘贴。",
    meta=APP_ONLY_META,
)
def copy_poster_clipboard(data_url: str) -> types.CallToolResult:
    prefix = "data:image/png;base64,"
    if not data_url or not data_url.startswith(prefix):
        return types.CallToolResult(
            isError=True,
            content=[types.TextContent(type="text", text="无效的 PNG 图片数据")],
        )
    b64_part = data_url[len(prefix) :]
    if len(b64_part) > 12 * 1024 * 1024:
        return types.CallToolResult(
            isError=True,
            content=[types.TextContent(type="text", text="图片体积超出限制")],
        )
    raw = base64.b64decode(b64_part)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="agy_poster_", suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)

        if sys.platform == "win32":
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
                "$bytes = [System.IO.File]::ReadAllBytes($env:AGY_CLIP_PNG); "
                "$ms = New-Object System.IO.MemoryStream(,$bytes); "
                "$img = [System.Drawing.Image]::FromStream($ms); "
                "$bmp = New-Object System.Drawing.Bitmap($img); "
                "[System.Windows.Forms.Clipboard]::SetImage($bmp); "
                "$img.Dispose(); $ms.Dispose();"
            )
            env = os.environ.copy()
            env["AGY_CLIP_PNG"] = tmp_path
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                err_msg = (proc.stderr or proc.stdout or "Clipboard failed").strip()
                return types.CallToolResult(
                    isError=True,
                    content=[types.TextContent(type="text", text=f"写入剪贴板失败：{err_msg}")],
                )
        elif sys.platform == "darwin":
            safe_p = tmp_path.replace('"', '\\"')
            proc = subprocess.run(
                ["osascript", "-e", f'set the clipboard to (read (POSIX file "{safe_p}") as «class PNGf»)'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                return types.CallToolResult(
                    isError=True,
                    content=[types.TextContent(type="text", text=f"写入 macOS 剪贴板失败：{proc.stderr.strip()}")],
                )
        elif shutil.which("wl-copy"):
            with open(tmp_path, "rb") as img_f:
                proc = subprocess.run(["wl-copy", "--type", "image/png"], stdin=img_f, capture_output=True, timeout=10)
            if proc.returncode != 0:
                return types.CallToolResult(
                    isError=True,
                    content=[types.TextContent(type="text", text="写入 Wayland 剪贴板失败，请使用「保存到图片目录」")],
                )
        elif shutil.which("xclip"):
            proc = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", tmp_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                return types.CallToolResult(
                    isError=True,
                    content=[types.TextContent(type="text", text="写入 X11 剪贴板失败，请使用「保存到图片目录」")],
                )
        else:
            return types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text="当前系统请使用浏览器快捷键或下载保存")],
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="已复制高清海报到系统剪贴板，可直接 Ctrl+V 粘贴")],
            structuredContent={"copied": True, "bytes": len(raw)},
        )
    except Exception as exc:
        return types.CallToolResult(
            isError=True,
            content=[types.TextContent(type="text", text=f"复制失败：{exc}")],
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def main():
    global DATA_DIR
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data_dir", default=None)
    args, _ = parser.parse_known_args()
    DATA_DIR = heatmap_stats.get_data_dir(args.data_dir)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
