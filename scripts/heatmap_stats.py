import glob
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import unquote

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)

APP_DATA_ROOTS = [
    os.path.join(os.path.expanduser("~"), ".gemini", "antigravity"),
    os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-ide"),
    os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli"),
]

MODEL_LABELS = {
    "gemini-4-hydrogen": "Gemini 4 Hydrogen",
    "gemini-4-neon": "Gemini 4 Neon",
    "gemini-4-argon": "Gemini 4 Argon",
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "gemini-flash-worker-agy": "Gemini Flash Worker",
    "orchestrator-neon": "Orchestrator (Neon)",
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
}

EFFORT_SUFFIXES = [
    ("-tiered-high", "Tiered"),
    ("-tiered-medium", "Tiered"),
    ("-tiered-low", "Tiered"),
    ("-tiered", "Tiered"),
    ("-xhigh", "XHigh"),
    ("-high", "High"),
    ("-medium", "Medium"),
    ("-low", "Low"),
]

_MEM_CACHE = {}


def get_data_dir(explicit_dir=None):
    data_dir = explicit_dir or os.environ.get("PLUGIN_DATA") or os.path.join(PLUGIN_ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _canonical_model(model_id: str):
    """Normalize thinking-effort variants into base model family while tracking effort tier."""
    if not model_id or model_id in ("unknown", "gemini-pro-orchestrator-neon", "orchestrator-neon"):
        return "orchestrator-neon", MODEL_LABELS["orchestrator-neon"], None
    low_id = model_id.strip().lower()
    effort = None
    base_id = low_id
    for suf, lbl in EFFORT_SUFFIXES:
        if low_id.endswith(suf):
            base_id = low_id[: -len(suf)]
            effort = lbl
            break
    label = MODEL_LABELS.get(base_id) or MODEL_LABELS.get(low_id)
    if not label:
        if base_id.startswith("model_placeholder_"):
            code = base_id[len("model_placeholder_") :].upper()
            label = f"Preview {code}"
        else:
            parts = base_id.replace("-4-6", "-4.6").replace("-5-5", "-5.5").split("-")
            label = " ".join(p.capitalize() if not p[0].isdigit() else p for p in parts)
    return base_id, label, effort


def _friendly_model(model_id: str) -> str:
    return _canonical_model(model_id)[1]


def _strip_prefixes(title: str) -> str:
    if not title:
        return ""
    s = re.sub(r"^(?:(?:🟢|🟡)\s*|\[?[🟠🔴]\s*\d+(?:\.\d+)?[kM]\]?\s*)+", "", title)
    s = re.sub(r"(?:\s*\[?[🟠🔴]\s*\d+(?:\.\d+)?[kM]\]?)+$", "", s)
    return s.strip()


def _parse_workspace_name(raw_ws: str) -> str:
    if not raw_ws:
        return ""
    uri = ""
    try:
        arr = json.loads(raw_ws)
        if isinstance(arr, list) and arr:
            uri = str(arr[0] or "")
    except Exception:
        uri = str(raw_ws or "")
    if not uri:
        return ""
    p = unquote(uri)
    if p.startswith("file:///"):
        p = p[8:]
    elif p.startswith("file://"):
        p = p[7:]
    p = p.rstrip("/\\")
    base = os.path.basename(p)
    return base or p


def _load_conv_meta():
    titles = {}
    children = set()
    workspaces = {}
    parent_map = {}
    max_nesting = 0
    for root in APP_DATA_ROOTS:
        db = os.path.join(root, "conversation_summaries.db")
        if os.path.exists(db):
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.5)
                for cid, title, parent, ws_raw, nest in conn.execute(
                    "SELECT conversation_id, title, parent_conversation_id, workspace_uris, nesting_depth FROM conversation_summaries"
                ):
                    if not cid:
                        continue
                    if parent:
                        children.add(cid)
                        parent_map.setdefault(cid, parent)
                    if nest and int(nest) > max_nesting:
                        max_nesting = int(nest)
                    if ws_raw and cid not in workspaces:
                        ws_name = _parse_workspace_name(ws_raw)
                        if ws_name:
                            workspaces[cid] = ws_name
                    if title and cid not in titles:
                        titles[cid] = _strip_prefixes(title)
                conn.close()
            except Exception:
                pass
        annot_dir = os.path.join(root, "annotations")
        if os.path.isdir(annot_dir):
            try:
                for fn in os.listdir(annot_dir):
                    if not fn.endswith(".pbtxt"):
                        continue
                    cid = fn[:-6]
                    if cid in titles:
                        continue
                    try:
                        with open(os.path.join(annot_dir, fn), "r", encoding="utf-8", errors="ignore") as f:
                            txt = f.read()
                        m = re.search(r'title:"((?:\\.|[^"\\])*)"', txt)
                        if m:
                            t = _strip_prefixes(m.group(1))
                            if t:
                                titles[cid] = t
                    except Exception:
                        pass
            except OSError:
                pass
    return titles, children, workspaces, parent_map, max_nesting


def _load_disk_cache(data_dir):
    cache_path = os.path.join(get_data_dir(data_dir), "transcript_cache_v6.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_disk_cache(data_dir, cache):
    cache_path = os.path.join(get_data_dir(data_dir), "transcript_cache_v6.json")
    tmp_path = f"{cache_path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp_path, cache_path)
    except Exception:
        pass


READ_TOOLS = {"read_file", "view_file", "view_file_outline"}
WRITE_TOOLS = {"edit_file", "write_file", "write_to_file", "replace_file_content", "multi_replace_file_content"}
FILE_TOOLS = READ_TOOLS | WRITE_TOOLS

KNOWN_BINS = {
    "python", "powershell", "pwsh", "node", "npm", "npx", "git", "curl", "rg",
    "ffprobe", "ffmpeg", "dotnet", "cargo", "go", "pip", "uv", "esbuild",
    "playwright", "adb", "sqlite3", "gh", "docker", "baw", "grep", "ls", "dir",
}
PS_CMDLETS = {
    "get-childitem", "get-content", "select-string", "get-process",
    "set-location", "write-host", "remove-item", "copy-item", "new-item",
    "move-item", "test-path", "invoke-webrequest", "invoke-restmethod",
    "powershell", "pwsh",
}

MODEL_PRICING_PER_M = {
    "claude-opus-5-5": (15.0, 1.50, 75.0),
    "claude-sonnet-5-5": (3.0, 0.30, 15.0),
    "claude-sonnet-4-6": (3.0, 0.30, 15.0),
    "gemini-4-hydrogen": (1.25, 0.31, 10.0),
    "gemini-4-neon": (1.25, 0.31, 10.0),
    "orchestrator-neon": (2.00, 0.25, 10.0),
    "gemini-4-argon": (0.30, 0.075, 2.50),
    "gemini-3.8-flash": (0.30, 0.075, 2.50),
    "gemini-flash-worker-agy": (0.30, 0.075, 2.50),
}
DEFAULT_PRICING_PER_M = (1.50, 0.20, 8.00)


def _calc_model_usd(canon_id: str, inp: int, ca: int, out: int):
    r_in, r_ca, r_out = MODEL_PRICING_PER_M.get(canon_id, DEFAULT_PRICING_PER_M)
    wo = ((inp + ca) * r_in + out * r_out) / 1_000_000.0
    act = (inp * r_in + ca * r_ca + out * r_out) / 1_000_000.0
    return wo, act, max(0.0, wo - act)


def _normalize_cmd_bin(cmd_line: str) -> str:
    s = str(cmd_line or "").strip()
    if not s:
        return ""
    low = s.lower()
    if "@'" in s and ("| python" in low or "python.exe" in low):
        return "python"
    if re.match(r"^(?:\\n|\s)*(?:import|from)\s+[a-z_]", low) or "nimport" in low[:24] or low.startswith("@\""):
        if "import " in low or "python" in low:
            return "python"
    # Strip common PowerShell preamble statements to uncover primary runtime binary
    parts = [p.strip() for p in s.split(";") if p.strip()]
    chosen = parts[0] if parts else s
    for p in parts:
        pl = p.lower()
        if (
            pl.startswith("$erroractionpreference")
            or pl.startswith("[console]::")
            or pl.startswith("chcp")
            or re.match(r"^(?:cd|set-location|pushd)\b", pl)
            or re.match(r"^\$env:", pl)
        ):
            continue
        chosen = p
        break
    first_tok = chosen.split()[0].strip("\"'&|()").lower() if chosen.split() else ""
    if not first_tok or first_tok.startswith("$") or first_tok in ("@", "@'", "{", "}", "#", "&"):
        if "python" in low:
            return "python"
        if "node " in low or "node.exe" in low:
            return "node"
        return ""
    base = os.path.basename(first_tok.replace("\\", "/"))
    if base.endswith(".exe"):
        base = base[:-4]
    if base in PS_CMDLETS:
        return "powershell"
    if base in ("python3", "py") or base.startswith("python3."):
        return "python"
    if base in KNOWN_BINS or re.match(r"^[a-z0-9][a-z0-9._-]{1,18}$", base):
        return base
    return ""


def _classify_error_kind(err_val) -> str:
    s = str(err_val or "").lower()
    if not s:
        return "other"
    if any(k in s for k in ("api error", "stream", "eof", "timeout", "request failed", "interrupted", "network", "status 5", "status 429")):
        return "api_net"
    if any(k in s for k in ("target content", "not found in the file", "target not found", "replace")):
        return "edit_match"
    if any(k in s for k in ("file does not exist", "cannot find path", "no such file", "enoent")):
        return "file_missing"
    return "other"


def _extract_file_info(tc_args):
    if not isinstance(tc_args, dict):
        return "", "", ""
    raw_p = tc_args.get("Path") or tc_args.get("TargetFile") or tc_args.get("AbsolutePath") or tc_args.get("path") or ""
    p = str(raw_p).strip().strip('"').strip("'").strip()
    if not p:
        return "", "", ""
    low = p.lower().replace("/", "\\")
    if "\\browser_sandbox\\" in low or "\\.system_generated\\" in low or "\\appdata\\local\\temp\\" in low:
        return "", "", ""
    _, ext = os.path.splitext(low)
    bn = os.path.basename(p.replace("\\", "/").rstrip("/"))
    return low, (ext if 1 < len(ext) <= 8 else ""), bn


def _parse_line_dt(d, mtime_float):
    ts_s = d.get("created_at") or ""
    if ts_s:
        try:
            return datetime.fromisoformat(ts_s.replace("Z", "+00:00")).astimezone()
        except Exception:
            pass
    return datetime.fromtimestamp(mtime_float).astimezone()


def _parse_transcript_for_heatmap(path: str):
    """Aggregate per-day, 7x24 punchcard, per-model, weekly evolution, tool/ext/MCP/shell/file/ctx & peak stats."""
    try:
        st = os.stat(path)
    except OSError:
        return None

    daily = {}       # YYYY-MM-DD -> [calls, in, cache, out]
    daily_ops = {}   # YYYY-MM-DD -> [tool_calls, thinking_calls]
    hourly = [0] * 24
    hourly_fresh = [0] * 24
    punchcard = [[0] * 24 for _ in range(7)]  # [0=Mon..6=Sun][0..23]
    weekly_models = {}  # YYYY-Www -> { raw_model -> fresh }
    models = {}      # model -> [calls, in, cache, out]
    tools = {}       # tool_name -> count
    mcp_servers = {} # server -> count
    mcp_tools = {}   # server/tool -> count
    cmd_bins = {}    # normalized bin -> count
    file_exts = {}   # ext -> count
    file_rw = {}     # basename -> [reads, writes, ext, sample_path]
    unique_files = set()
    error_cats = {"api_net": 0, "edit_match": 0, "file_missing": 0, "other": 0}
    thinking_calls = 0
    thinking_chars = 0
    user_prompts = 0
    user_chars = 0
    error_events = 0
    checkpoints = 0
    max_ctx = 0
    max_single_out = 0
    max_single_in = 0
    conv_calls = 0
    weekend_calls = 0
    media_events = 0
    epochs = []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            last_model = ""
            for line in f:
                if (
                    '"input_tokens"' not in line
                    and '"output_tokens"' not in line
                    and '"tool_calls"' not in line
                    and '"media"' not in line
                    and '"USER_INPUT"' not in line
                    and '"USER_EXPLICIT"' not in line
                    and '"error"' not in line
                    and '"CHECKPOINT"' not in line
                ):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue

                if d.get("type") == "CHECKPOINT":
                    checkpoints += 1

                if d.get("error") or d.get("status") == "ERROR":
                    error_events += 1
                    ek = _classify_error_kind(d.get("error"))
                    error_cats[ek] = error_cats.get(ek, 0) + 1

                src = d.get("source")
                typ = d.get("type")
                if src == "USER_EXPLICIT" or typ == "USER_INPUT":
                    txt = str(d.get("content") or "").strip()
                    if txt:
                        user_prompts += 1
                        user_chars += len(txt)

                tcs = d.get("tool_calls") or []
                th = d.get("thinking")
                in_t = int(d.get("input_tokens") or 0)
                ca_t = int(d.get("cache_read_tokens") or 0)
                ou_t = int(d.get("output_tokens") or 0)

                need_dt = bool((isinstance(tcs, list) and tcs) or th or in_t or ca_t or ou_t)
                dt = _parse_line_dt(d, st.st_mtime) if need_dt else None
                day_k = dt.strftime("%Y-%m-%d") if dt else ""

                if isinstance(tcs, list) and tcs:
                    tc_in_line = 0
                    for tc in tcs:
                        if isinstance(tc, dict):
                            nm = tc.get("name") or (tc.get("tool") or {}).get("name") or ""
                            if nm:
                                tc_in_line += 1
                                tools[nm] = tools.get(nm, 0) + 1
                                tc_args = tc.get("args")
                                if nm in FILE_TOOLS:
                                    fpath, ext, bn = _extract_file_info(tc_args)
                                    if fpath:
                                        unique_files.add(fpath)
                                    if ext:
                                        file_exts[ext] = file_exts.get(ext, 0) + 1
                                    if bn:
                                        frw = file_rw.get(bn)
                                        if not frw:
                                            frw = [0, 0, ext, fpath]
                                            file_rw[bn] = frw
                                        if nm in READ_TOOLS:
                                            frw[0] += 1
                                        else:
                                            frw[1] += 1
                                        if not frw[2] and ext:
                                            frw[2] = ext
                                elif nm == "call_mcp_tool" and isinstance(tc_args, dict):
                                    srv_n = str(tc_args.get("ServerName") or "").strip().strip('"').strip("'")
                                    t_n = str(tc_args.get("ToolName") or "").strip().strip('"').strip("'")
                                    if srv_n:
                                        mcp_servers[srv_n] = mcp_servers.get(srv_n, 0) + 1
                                        if t_n:
                                            mt_k = f"{srv_n}/{t_n}"
                                            mcp_tools[mt_k] = mcp_tools.get(mt_k, 0) + 1
                                elif nm == "run_command" and isinstance(tc_args, dict):
                                    cbin = _normalize_cmd_bin(tc_args.get("CommandLine") or "")
                                    if cbin:
                                        cmd_bins[cbin] = cmd_bins.get(cbin, 0) + 1
                    if tc_in_line and day_k:
                        op_row = daily_ops.get(day_k)
                        if not op_row:
                            op_row = [0, 0]
                            daily_ops[day_k] = op_row
                        op_row[0] += tc_in_line

                if th:
                    thinking_calls += 1
                    thinking_chars += len(str(th))
                    if day_k:
                        op_row = daily_ops.get(day_k)
                        if not op_row:
                            op_row = [0, 0]
                            daily_ops[day_k] = op_row
                        op_row[1] += 1

                med = d.get("media")
                if isinstance(med, list):
                    media_events += len(med)

                if not (in_t or ca_t or ou_t):
                    continue

                conv_calls += 1
                ctx_sz = in_t + ca_t
                if ctx_sz > max_ctx:
                    max_ctx = ctx_sz
                if ou_t > max_single_out:
                    max_single_out = ou_t
                if in_t > max_single_in:
                    max_single_in = in_t

                model = d.get("model") or last_model or "unknown"
                if d.get("model"):
                    last_model = d["model"]

                m_row = models.get(model)
                if not m_row:
                    m_row = [0, 0, 0, 0]
                    models[model] = m_row
                m_row[0] += 1
                m_row[1] += in_t
                m_row[2] += ca_t
                m_row[3] += ou_t

                epochs.append(dt.timestamp())
                d_row = daily.get(day_k)
                if not d_row:
                    d_row = [0, 0, 0, 0]
                    daily[day_k] = d_row
                d_row[0] += 1
                d_row[1] += in_t
                d_row[2] += ca_t
                d_row[3] += ou_t

                wd = dt.weekday()
                hr = dt.hour
                hourly[hr] += 1
                hourly_fresh[hr] += in_t + ou_t
                punchcard[wd][hr] += 1
                if wd >= 5:
                    weekend_calls += 1

                iso_y, iso_w, _ = dt.isocalendar()
                wk_k = f"{iso_y}-W{iso_w:02d}"
                wm = weekly_models.get(wk_k)
                if not wm:
                    wm = {}
                    weekly_models[wk_k] = wm
                wm[model] = wm.get(model, 0) + in_t + ou_t
    except Exception:
        return None

    longest_flow_sec = 0
    total_flow_sec = 0
    if len(epochs) >= 2:
        epochs.sort()
        cur_flow = 0
        for i in range(1, len(epochs)):
            gap = epochs[i] - epochs[i - 1]
            if 0 <= gap <= 1500:
                cur_flow += gap
                total_flow_sec += gap
                if cur_flow > longest_flow_sec:
                    longest_flow_sec = cur_flow
            else:
                cur_flow = 0

    day_keys = sorted(daily.keys())
    return {
        "v6": 2,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "daily": daily,
        "daily_ops": daily_ops,
        "first_day": day_keys[0] if day_keys else "",
        "last_day": day_keys[-1] if day_keys else "",
        "hourly": hourly,
        "hourly_fresh": hourly_fresh,
        "punchcard": punchcard,
        "weekly_models": weekly_models,
        "models": models,
        "tools": tools,
        "mcp_servers": mcp_servers,
        "mcp_tools": mcp_tools,
        "cmd_bins": cmd_bins,
        "file_exts": file_exts,
        "file_rw": file_rw,
        "unique_files": sorted(unique_files),
        "thinking_calls": thinking_calls,
        "thinking_chars": thinking_chars,
        "user_prompts": user_prompts,
        "user_chars": user_chars,
        "error_events": error_events,
        "error_cats": error_cats,
        "checkpoints": checkpoints,
        "max_ctx": max_ctx,
        "longest_flow_sec": round(longest_flow_sec),
        "total_flow_sec": round(total_flow_sec),
        "max_single_out": max_single_out,
        "max_single_in": max_single_in,
        "conv_calls": conv_calls,
        "weekend_calls": weekend_calls,
        "media_events": media_events,
    }


def _compute_streaks(active_day_keys: list[str], today_str: str):
    if not active_day_keys:
        return 0, 0
    day_dates = sorted({datetime.strptime(d, "%Y-%m-%d").date() for d in active_day_keys})
    longest = 1
    cur = 1
    for i in range(1, len(day_dates)):
        if (day_dates[i] - day_dates[i - 1]).days == 1:
            cur += 1
            if cur > longest:
                longest = cur
        else:
            cur = 1

    today_d = datetime.strptime(today_str, "%Y-%m-%d").date()
    day_set = set(day_dates)
    current_streak = 0
    cursor = today_d if today_d in day_set else (today_d - timedelta(days=1))
    while cursor in day_set:
        current_streak += 1
        cursor -= timedelta(days=1)

    return current_streak, longest


def _chronotype_label(hourly_calls: list[int]):
    total = sum(hourly_calls)
    if total == 0:
        return "owl", "蓄力观察", "0%", 0
    peak_hr = max(range(24), key=lambda h: hourly_calls[h])
    late_night = sum(hourly_calls[0:6])
    evening = sum(hourly_calls[19:24])
    morning = sum(hourly_calls[6:12])
    afternoon = sum(hourly_calls[12:19])

    late_pct = round((late_night / total) * 100, 1)
    if peak_hr in range(0, 6) or late_pct >= 28:
        return "owl", "凌晨战神", f"00–05 占比 {late_pct}% · 峰值 {peak_hr:02d}:00", peak_hr
    if evening >= max(morning, afternoon, late_night):
        return "moon", "夜航架构师", f"19–24 黄金时段 · 峰值 {peak_hr:02d}:00", peak_hr
    if morning >= max(afternoon, evening, late_night):
        return "sun", "晨曦开发者", f"06–11 黄金时段 · 峰值 {peak_hr:02d}:00", peak_hr
    return "bolt", "午后全速极客", f"12–18 黄金时段 · 峰值 {peak_hr:02d}:00", peak_hr


TIER_NAMES = ["未解锁", "青铁", "精钢", "青铜", "白银", "黄金", "铂金", "翡翠", "紫晶", "星钻", "传奇"]


def _tiered_badge(
    bid: str,
    category: str,
    cat_name: str,
    shape: str,
    title: str,
    desc: str,
    cur,
    tiers: list,
    unit: str,
):
    level = 0
    for th in tiers:
        if cur >= th:
            level += 1
        else:
            break
    target = tiers[level] if level < len(tiers) else tiers[-1]
    pct = 100 if level >= len(tiers) else min(99, max(0, round((cur / max(1e-9, target)) * 100)))
    return {
        "id": bid,
        "category": category,
        "categoryName": cat_name,
        "shape": shape,
        "title": title,
        "desc": desc,
        "cur": cur,
        "target": target,
        "tiers": tiers,
        "tierNames": TIER_NAMES[1:],
        "unit": unit,
        "level": level,
        "maxLevel": len(tiers),
        "tier": TIER_NAMES[level],
        "unlocked": level >= 1,
        "pct": pct,
    }


def _build_achievements(summary, peak_day, tools_summary, hourly_calls, extra):
    late_calls = sum(hourly_calls[0:6])
    morning_calls = sum(hourly_calls[6:12])
    afternoon_calls = sum(hourly_calls[12:19])
    evening_calls = sum(hourly_calls[19:24])
    peak_fresh_m = round((peak_day["fresh"] if peak_day else 0) / 1_000_000, 1)

    specs = [
        # 1. 创作工程 (shield) — 9 项独立创作与工程维度（门槛进阶校准，含高阶史诗工程挑战）
        ("out_total", "output", "创作工程", "shield", "著作等身", "累计生成输出 Output Tokens (M)", round(summary["totalOutput"] / 1_000_000, 1), [5, 15, 35, 75, 150, 300, 600, 1100, 2000, 3500], "M"),
        ("eng_prompts", "output", "创作工程", "shield", "提问大师", "亲手发起的需求与指令总数", extra["userPrompts"], [150, 500, 1200, 2500, 5000, 10000, 20000, 38000, 70000, 120000], "次"),
        ("eng_prompt_chars", "output", "创作工程", "shield", "需求雕刻师", "亲手敲下的需求总字数 (k字)", extra["userCharsK"], [20, 80, 200, 500, 1100, 2200, 4500, 8500, 15000, 25000], "k字"),
        ("eng_unique_files", "output", "创作工程", "shield", "工程拓荒者", "实战读写触达的独立工程文件数", extra["uniqueFiles"], [60, 180, 450, 1000, 2200, 4500, 8500, 15000, 26000, 45000], "个"),
        ("eng_main_convs", "output", "创作工程", "shield", "百战项目官", "发起并推进的独立主任务总数", summary["mainConvs"], [15, 45, 100, 220, 450, 850, 1500, 2600, 4200, 7000], "场"),
        ("eng_epic_convs", "output", "创作工程", "shield", "史诗工程匠", "深度打磨 ≥ 300 轮大工程数", extra["epicConvs"], [3, 8, 18, 35, 65, 115, 190, 300, 460, 700], "场"),
        ("eng_mega_convs", "output", "创作工程", "shield", "千轮巨舰长", "单场打磨 ≥ 1,000 轮超级工程数", extra["megaConvs"], [3, 7, 14, 24, 38, 60, 92, 140, 210, 320], "场"),
        ("eng_new_files", "output", "创作工程", "shield", "创世代码手", "从零新建落盘的源码与文档次数", extra["newFileWrites"], [120, 400, 1000, 2400, 5500, 12000, 24000, 45000, 80000, 140000], "次"),
        ("eng_conv_files", "output", "创作工程", "shield", "大仓手术刀", "单场工程触达独立文件数峰值", extra["maxConvFiles"], [60, 160, 320, 580, 950, 1450, 2100, 3000, 4200, 6000], "个"),

        # 2. 算力实战 (hex) — 9 项 100% 用户行为驱动的高强度实战维度
        ("tp_fresh_total", "throughput", "算力实战", "hex", "算力引擎", "累计全新净消耗算力 (M)", round(summary["totalFresh"] / 1_000_000, 1), [30, 100, 260, 600, 1300, 2600, 5000, 9500, 17000, 30000], "M"),
        ("tp_calls", "throughput", "算力实战", "hex", "对话万象", "累计模型总交互轮数 (k)", round(summary["totalCalls"] / 1000, 1), [3, 10, 25, 55, 110, 210, 380, 650, 1100, 1800], "k"),
        ("tp_active_weeks", "throughput", "算力实战", "hex", "周更长征", "历史活跃跨越独立自然周数", extra["activeWeeks"], [2, 5, 10, 18, 30, 48, 72, 104, 150, 208], "周"),
        ("tp_active_months", "throughput", "算力实战", "hex", "月度常青树", "历史活跃跨越独立自然月数", extra["activeMonths"], [3, 6, 10, 15, 22, 30, 40, 52, 68, 84], "月"),
        ("tp_peak_week", "throughput", "算力实战", "hex", "单周爆发王", "单自然周最高净消耗纪录 (M)", extra["maxWeekFreshM"], [20, 50, 100, 180, 300, 480, 720, 1050, 1500, 2200], "M"),
        ("tp_focus_days", "throughput", "算力实战", "hex", "五百轮战日", "单日深度交互 ≥ 500 轮天数", extra["daysOver500Calls"], [3, 8, 18, 35, 65, 110, 180, 280, 420, 600], "天"),
        ("tp_hyper_days", "throughput", "算力实战", "hex", "千轮烈焰日", "单日高燃交互 ≥ 1,000 轮天数", extra["daysOver1000Calls"], [5, 12, 25, 45, 80, 130, 200, 300, 450, 650], "天"),
        ("tp_debug_kills", "throughput", "算力实战", "hex", "除错猎手", "实战排查并攻克报错异常回合", extra["errorRecoveries"], [60, 200, 500, 1200, 2600, 5500, 11000, 20000, 35000, 60000], "次"),
        ("tp_big_input", "throughput", "算力实战", "hex", "万卷视野", "单次吞入超长新上下文峰值 (k)", extra["maxSingleInK"], [80, 160, 300, 500, 780, 1150, 1600, 2200, 3000, 4000], "k"),

        # 3. 毅力心流 (laurel) — 9 项永久单调递增的作息与心流耐力维度
        ("str_longest", "rhythm", "毅力心流", "laurel", "铁军连击", "历史最长连续活跃天数", summary["longestStreak"], [5, 10, 20, 35, 60, 100, 160, 240, 365, 550], "天"),
        ("flow_total", "rhythm", "毅力心流", "laurel", "深度心流者", "累计连续编码心流总时长 (h)", extra["totalFlowHours"], [15, 50, 140, 320, 680, 1300, 2400, 4200, 7000, 11000], "h"),
        ("flow_longest", "rhythm", "毅力心流", "laurel", "不眠马拉松", "单次最长不间断心流时长 (h)", extra["longestFlowHours"], [2, 4, 7, 11, 16, 22, 29, 36, 44, 56], "h"),
        ("act_days", "rhythm", "毅力心流", "laurel", "日积月累", "历史累计活跃总天数", summary["activeDays"], [7, 18, 40, 80, 150, 260, 420, 650, 950, 1400], "天"),
        ("hr_midnight", "rhythm", "毅力心流", "laurel", "凌晨守护者", "00:00–05:59 深夜专注交互", late_calls, [600, 2000, 5500, 13000, 28000, 55000, 105000, 190000, 320000, 500000], "次"),
        ("hr_morning", "rhythm", "毅力心流", "laurel", "晨曦早起鸟", "06:00–11:59 晨间专注交互", morning_calls, [1000, 3500, 9000, 20000, 42000, 85000, 160000, 280000, 460000, 720000], "次"),
        ("hr_afternoon", "rhythm", "毅力心流", "laurel", "午后全速机", "12:00–18:59 午后持续实战交互", afternoon_calls, [1500, 5000, 13000, 30000, 65000, 130000, 240000, 420000, 700000, 1100000], "次"),
        ("hr_evening", "rhythm", "毅力心流", "laurel", "暮色攻坚者", "19:00–23:59 黄金夜战专注交互", evening_calls, [1500, 5000, 13000, 30000, 65000, 130000, 240000, 420000, 700000, 1100000], "次"),
        ("act_weekend", "rhythm", "毅力心流", "laurel", "周末极客", "周六周日累计高燃交互", extra["weekendCalls"], [1000, 3500, 9000, 22000, 48000, 95000, 180000, 320000, 540000, 850000], "次"),

        # 4. 工具掌控 (diamond) — 9 项独立全栈自动化与生态技能
        ("tl_total", "tools", "工具掌控", "diamond", "自动化交响乐", "全工具自动化调用总数", tools_summary["total"], [3000, 10000, 28000, 65000, 140000, 280000, 520000, 920000, 1500000, 2400000], "次"),
        ("tl_cmd", "tools", "工具掌控", "diamond", "终端指挥官", "驱动执行终端命令次数", tools_summary["commands"], [1500, 5000, 14000, 32000, 70000, 140000, 260000, 460000, 780000, 1250000], "次"),
        ("tl_write", "tools", "工具掌控", "diamond", "代码铸剑师", "精准局部代码编辑与微雕改写", extra["editOps"], [300, 1100, 3000, 7500, 16000, 34000, 68000, 130000, 230000, 380000], "次"),
        ("tl_read", "tools", "工具掌控", "diamond", "代码透视眼", "工程源码阅读与检视数", tools_summary["reads"], [1000, 3500, 9500, 22000, 48000, 95000, 180000, 320000, 540000, 880000], "次"),
        ("tl_web", "tools", "工具掌控", "diamond", "全网情报官", "实时网页搜索与抓取数", tools_summary["webSearches"], [300, 1000, 2800, 6500, 14000, 28000, 55000, 100000, 175000, 280000], "次"),
        ("tl_browser", "tools", "工具掌控", "diamond", "浏览器驯兽师", "驱动内置浏览器真机实操数", tools_summary["browserOps"], [150, 500, 1400, 3500, 7500, 15000, 30000, 55000, 95000, 160000], "次"),
        ("tl_mcp", "tools", "工具掌控", "diamond", "生态拓展师", "调用 MCP 外部插件与资源次数", extra["mcpCalls"], [100, 300, 800, 1800, 3800, 7500, 14000, 25000, 42000, 70000], "次"),
        ("tl_tasks", "tools", "工具掌控", "diamond", "异步总控师", "后台任务管控与定时调度次数", extra["taskOps"], [250, 800, 2200, 5200, 11500, 23000, 44000, 80000, 140000, 230000], "次"),
        ("tl_conv_peak", "tools", "工具掌控", "diamond", "单场工具风暴", "单场对话最高工具连发纪录", extra["maxConvTools"], [350, 900, 2000, 3800, 6500, 10500, 16000, 24000, 35000, 50000], "次"),

        # 5. 智能协同 (octagon) — 9 项独立深度推演、多代理与高阶协同能力
        ("ai_think_chars", "agents", "智能协同", "octagon", "超级大脑共振", "AI 深度推演思考总万字数", round(summary["thinkingChars"] / 10000), [150, 450, 1100, 2400, 4800, 9000, 16000, 28000, 48000, 78000], "万字"),
        ("ai_think_calls", "agents", "智能协同", "octagon", "千思百虑", "触发大模型深度思考总次数", summary["thinkingCalls"], [1500, 5000, 13000, 30000, 65000, 130000, 240000, 420000, 700000, 1100000], "次"),
        ("ag_subagents", "agents", "智能协同", "octagon", "多代理统帅", "派出独立子代理总数", summary["subConvs"], [10, 30, 75, 160, 320, 600, 1050, 1800, 3000, 4800], "个"),
        ("ag_swarm_peak", "agents", "智能协同", "octagon", "蜂群指挥官", "单任务最高并行派生子代理数", extra["maxSwarmChildren"], [2, 5, 10, 18, 28, 42, 60, 85, 115, 160], "个"),
        ("ag_messages", "agents", "智能协同", "octagon", "集群通信官", "代理调度与跨代理消息协同数", extra["agentMsgs"], [60, 200, 550, 1300, 2800, 5800, 11000, 20000, 35000, 58000], "次"),
        ("pl_plans", "agents", "智能协同", "octagon", "蓝图架构师", "规划模式与步骤清单拆解次数", extra["planOps"], [100, 350, 950, 2200, 4800, 9500, 18000, 32000, 55000, 90000], "次"),
        ("mm_media", "agents", "智能协同", "octagon", "全能跨模态", "识图审阅与多模态素材交互数", extra["mediaEvents"], [300, 1000, 2800, 6500, 14000, 28000, 55000, 100000, 175000, 290000], "次"),
        ("mm_imggen", "agents", "智能协同", "octagon", "视觉造物主", "驱动 AI 图像生成创作张数", extra["imgGenCnt"], [50, 150, 400, 900, 1900, 3800, 7200, 13000, 22000, 36000], "张"),
        ("ag_workflows", "agents", "智能协同", "octagon", "编排炼金师", "自动化工作流与定时巡检次数", extra["workflowOps"], [150, 450, 1100, 2500, 5500, 11000, 20000, 36000, 62000, 100000], "次"),

        # 6. 巅峰传奇 (crest) — 9 项高含金量纪录丰碑（含传奇级未解锁挑战）
        ("pk_day_fresh", "milestones", "巅峰传奇", "crest", "单日狂飙", "单日净消耗巅峰纪录 (M)", peak_fresh_m, [8, 18, 32, 50, 75, 110, 155, 210, 280, 380], "M"),
        ("pk_day_output", "milestones", "巅峰传奇", "crest", "日产雄文", "单日生成输出峰值纪录 (M)", extra["maxDayOutM"], [1.5, 3.5, 6.5, 11, 17, 25, 35, 48, 65, 88], "M"),
        ("pk_day_calls", "milestones", "巅峰传奇", "crest", "千战传奇", "单日模型交互轮数巅峰纪录", extra["maxDayCalls"], [300, 700, 1300, 2200, 3500, 5200, 7500, 10500, 14500, 20000], "次"),
        ("pk_10m_days", "milestones", "巅峰传奇", "crest", "千万俱乐部", "单日净消耗 ≥ 10M 高燃天数", extra["daysOver10M"], [3, 8, 18, 35, 65, 110, 180, 280, 420, 600], "天"),
        ("pk_30m_days", "milestones", "巅峰传奇", "crest", "三千万核爆日", "单日净消耗 ≥ 30M 核级爆肝天数", extra["daysOver30M"], [3, 7, 15, 28, 48, 78, 120, 180, 260, 380], "天"),
        ("pk_50m_days", "milestones", "巅峰传奇", "crest", "五千万神迹日", "单日净消耗 ≥ 50M 传说级爆肝天数", extra["daysOver50M"], [1, 3, 6, 12, 22, 36, 56, 85, 125, 180], "天"),
        ("pk_conv_fresh", "milestones", "巅峰传奇", "crest", "巨兽工程纪录", "单场史诗对话净消耗巅峰 (M)", extra["maxConvFreshM"], [8, 20, 40, 70, 115, 175, 255, 360, 500, 700], "M"),
        ("cv_marathon", "milestones", "巅峰传奇", "crest", "深水马拉松", "单场最长深度交互轮数", extra["maxConvCalls"], [200, 500, 1100, 2200, 4000, 6800, 10500, 16000, 24000, 36000], "次"),
        ("pk_1m_out_days", "milestones", "巅峰传奇", "crest", "百万笔锋", "单日生成输出 ≥ 1M 高产天数", extra["daysOver1MOut"], [3, 8, 18, 35, 65, 110, 180, 280, 420, 600], "天"),
    ]
    return [_tiered_badge(*sp) for sp in specs]


def _load_plugin_repo():
    pj = os.path.join(PLUGIN_ROOT, "plugin.json")
    try:
        with open(pj, "r", encoding="utf-8") as f:
            meta = json.load(f)
        repo = meta.get("repository")
        if isinstance(repo, str):
            return repo.strip()
        if isinstance(repo, dict):
            return str(repo.get("url") or "").strip()
    except Exception:
        pass
    return ""


def load_ui_prefs(data_dir=None):
    p = os.path.join(get_data_dir(data_dir), "prefs.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            g = raw.get("granularity", 50)
            tm = raw.get("themeMode", "auto")
            if tm not in ("auto", "dark", "light"):
                tm = "auto"
            return {"granularity": max(0, min(100, int(g))), "themeMode": tm, "saved": True}
    except Exception:
        pass
    return {"granularity": 50, "themeMode": "auto", "saved": False}


def save_ui_prefs(data_dir=None, granularity=None, theme_mode=None):
    cur = load_ui_prefs(data_dir)
    if granularity is not None:
        cur["granularity"] = max(0, min(100, int(granularity)))
    if theme_mode in ("auto", "dark", "light"):
        cur["themeMode"] = theme_mode
    cur["saved"] = True
    p = os.path.join(get_data_dir(data_dir), "prefs.json")
    tmp = f"{p}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"granularity": cur["granularity"], "themeMode": cur["themeMode"]}, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass
    _MEM_CACHE.pop("result_sig", None)
    return cur


TOOL_LABELS = {
    "run_command": "终端命令",
    "read_file": "源码阅读",
    "edit_file": "精准编辑",
    "write_file": "文件写入",
    "manage_task": "后台任务",
    "search_web": "联网检索",
    "view_file": "代码检视",
    "view_file_outline": "结构大纲",
    "webview_browser": "浏览器操控",
    "update_focus": "状态聚焦",
    "update_steps": "步骤清单",
    "write_to_file": "新建落盘",
    "replace_file_content": "段落替换",
    "multi_replace_file_content": "批量重构",
    "send_message": "跨代理通信",
    "exit_plan_mode": "方案定稿",
    "enter_plan_mode": "进入规划",
    "schedule": "定时调度",
    "invoke_subagent": "派发子代理",
    "manage_subagents": "代理管控",
    "call_mcp_tool": "MCP 插件",
    "generate_image": "AI 生图",
    "ask_question": "需求澄清",
    "search_marketplace": "插件检索",
    "read_url_content": "网页精读",
}

ARCHETYPE_DEFS = {
    "swarm_arch": ("集群编排", "#2dd4bf", "派生子代理、规划拆解或跨模块架构重构"),
    "ui_visual": ("视觉与前端", "#f472b6", "前端界面、样式精雕、海报/矢量图或浏览器视觉调试"),
    "debug_fix": ("攻坚排错", "#fb923c", "高报错自愈密度、故障定位与缺陷修复"),
    "research": ("调研侦察", "#38bdf8", "联网检索、资料核验与大比例源码研读分析"),
    "ops_auto": ("脚本运维", "#818cf8", "命令行驱动占比高、自动化脚本、定时调度或 MCP 运维"),
    "feature_build": ("功能构筑", "#3de066", "新功能源码编写、核心模块迭代与工程落地"),
}


def _classify_conv_archetype(title: str, tools: dict, file_exts: dict, errors: int, calls: int, children: int) -> str:
    t = (title or "").lower()
    tot_tools = max(1, sum(int(v) for v in (tools or {}).values()))
    cmd_c = int((tools or {}).get("run_command", 0))
    read_c = sum(int((tools or {}).get(k, 0)) for k in READ_TOOLS)
    write_c = sum(int((tools or {}).get(k, 0)) for k in WRITE_TOOLS)
    web_c = int((tools or {}).get("search_web", 0)) + int((tools or {}).get("read_url_content", 0))
    browser_c = int((tools or {}).get("webview_browser", 0)) + sum(int(v) for k, v in (tools or {}).items() if str(k).startswith("browser_"))
    plan_c = sum(int((tools or {}).get(k, 0)) for k in ("enter_plan_mode", "exit_plan_mode", "update_steps", "invoke_subagent", "run_workflow"))
    mcp_c = int((tools or {}).get("call_mcp_tool", 0)) + int((tools or {}).get("schedule", 0))
    imggen_c = int((tools or {}).get("generate_image", 0))
    ui_ext_c = sum(int((file_exts or {}).get(k, 0)) for k in (".html", ".htm", ".css", ".scss", ".jsx", ".tsx", ".svg"))

    if children >= 2 or int((tools or {}).get("invoke_subagent", 0)) >= 2 or plan_c >= 6 or any(k in t for k in ("集群", "swarm", "规划", "架构", "交接")):
        return "swarm_arch"
    if imggen_c >= 1 or browser_c >= 6 or ui_ext_c >= max(4, (read_c + write_c) * 0.35) or any(k in t for k in ("ui", "海报", "热力图", "前端", "样式", "视觉", "界面", "css", "拼豆", "pindou", "美化", "勋章")):
        return "ui_visual"
    if errors >= 6 or (calls > 0 and errors / max(1, calls) >= 0.08) or any(k in t for k in ("fix", "bug", "卡住", "报错", "修复", "排查", "失败", "调试", "debug", "清理")):
        return "debug_fix"
    if (web_c >= 8 and web_c / tot_tools >= 0.14) or (read_c > write_c * 3.2 and read_c >= 15) or any(k in t for k in ("分析", "研究", "调研", "阅读", "查找", "探索", "review")):
        return "research"
    if (cmd_c / tot_tools >= 0.60 and write_c <= cmd_c * 0.28) or mcp_c >= 5 or any(k in t for k in ("自动", "脚本", "监控", "额度", "配置", "部署", "cli", "同步")):
        return "ops_auto"
    return "feature_build"


def _pct_delta(cur: int, prev: int):
    if prev <= 0:
        return None
    val = round(((cur - prev) / prev) * 100)
    return max(-99, min(999, val))


EXT_LANG_MAP = {
    ".py": ("Python", "#3572A5"),
    ".js": ("JavaScript", "#f7df1e"),
    ".mjs": ("JavaScript", "#f7df1e"),
    ".cjs": ("JavaScript", "#f7df1e"),
    ".jsx": ("React JSX", "#61dafb"),
    ".ts": ("TypeScript", "#3178c6"),
    ".tsx": ("TypeScript", "#3178c6"),
    ".html": ("HTML", "#e34c26"),
    ".htm": ("HTML", "#e34c26"),
    ".css": ("CSS", "#a855f7"),
    ".scss": ("CSS", "#a855f7"),
    ".json": ("JSON", "#10b981"),
    ".md": ("Markdown", "#38bdf8"),
    ".ps1": ("PowerShell", "#22d3ee"),
    ".sh": ("Shell", "#22d3ee"),
    ".bat": ("Shell", "#22d3ee"),
    ".cmd": ("Shell", "#22d3ee"),
    ".cs": ("C#", "#178600"),
    ".cpp": ("C++", "#f34b7d"),
    ".c": ("C", "#64748b"),
    ".h": ("C/C++", "#f34b7d"),
    ".rs": ("Rust", "#dea584"),
    ".go": ("Go", "#00ADD8"),
    ".java": ("Java", "#b07219"),
    ".sql": ("SQL", "#e38c00"),
    ".yaml": ("YAML", "#cb171e"),
    ".yml": ("YAML", "#cb171e"),
    ".toml": ("TOML", "#9c4221"),
    ".svg": ("SVG", "#ffb13b"),
}


def collect_heatmap_data(data_dir=None):
    data_dir = get_data_dir(data_dir)
    candidates = []
    for root in APP_DATA_ROOTS:
        pattern = os.path.join(root, "brain", "*", ".system_generated", "logs", "transcript.jsonl")
        for fp in glob.glob(pattern):
            cid = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(fp))))
            candidates.append((cid, fp))

    # Fast filesystem stat fingerprint for fast warm-cache hits (~60% fewer ms on Windows)
    today_now_str = datetime.now().strftime("%Y-%m-%d")
    uniq_stat = []
    seen_stat_cids = set()
    sum_size = 0
    max_mtime = 0.0
    for cid, fp in candidates:
        if cid in seen_stat_cids:
            continue
        seen_stat_cids.add(cid)
        try:
            st = os.stat(fp)
            uniq_stat.append((cid, fp, st))
            sum_size += st.st_size
            if st.st_mtime > max_mtime:
                max_mtime = st.st_mtime
        except OSError:
            continue

    # Check only the first root brain dir mtime to keep syscall overhead minimal; fallback to individual stats
    prefs_path = os.path.join(data_dir, "prefs.json")
    try:
        prefs_mtime = os.stat(prefs_path).st_mtime
    except OSError:
        prefs_mtime = 0.0

    sig = (data_dir, today_now_str, len(uniq_stat), sum_size, max_mtime, prefs_mtime)
    if _MEM_CACHE.get("result_sig") == sig and _MEM_CACHE.get("result_data") is not None:
        return _MEM_CACHE["result_data"]

    titles, child_cids, conv_workspaces, parent_map, max_nesting = _load_conv_meta()

    disk_cache = _MEM_CACHE.get("disk")
    if disk_cache is None:
        disk_cache = _load_disk_cache(data_dir)
        _MEM_CACHE["disk"] = disk_cache

    cache_dirty = False
    daily_agg = {}
    daily_ops_agg = {}
    hourly_calls = [0] * 24
    hourly_fresh = [0] * 24
    punchcard_agg = [[0] * 24 for _ in range(7)]
    weekly_models_agg = {}
    models_agg = {}
    tools_agg = {}
    mcp_servers_agg = {}
    mcp_tools_agg = {}
    cmd_bins_agg = {}
    file_rw_agg = {}
    ext_agg = {}
    unique_files_agg = set()
    workspaces_agg = {}
    conv_rows = []
    conv_detail_raw = {}
    conv_peak_ctxs = []
    error_cats_agg = {"api_net": 0, "edit_match": 0, "file_missing": 0, "other": 0}
    parent_children_agg = {}

    total_thinking_calls = 0
    total_thinking_chars = 0
    total_user_prompts = 0
    total_user_chars = 0
    total_error_events = 0
    total_checkpoints = 0
    longest_flow_sec = 0
    total_flow_sec = 0
    max_single_out = 0
    max_single_in = 0
    max_conv_calls = 0
    max_conv_tools = 0
    max_conv_files = 0
    weekend_calls = 0
    total_media_events = 0
    main_conv_count = 0
    sub_conv_count = 0

    for cid, fp, st in uniq_stat:
        cached = disk_cache.get(fp)
        if cached and cached.get("mtime") == st.st_mtime and cached.get("size") == st.st_size and cached.get("v6") == 2:
            parsed = cached
        else:
            parsed = _parse_transcript_for_heatmap(fp)
            if not parsed:
                continue
            disk_cache[fp] = parsed
            cache_dirty = True

        if not parsed.get("daily"):
            continue

        is_child = cid in child_cids
        if is_child:
            sub_conv_count += 1
            p_cid = parent_map.get(cid)
            if p_cid:
                parent_children_agg[p_cid] = parent_children_agg.get(p_cid, 0) + 1
        else:
            main_conv_count += 1

        conv_title = titles.get(cid) or (f"子代理 {cid[:6]}" if is_child else f"对话 {cid[:8]}")
        ws_name = conv_workspaces.get(cid) or ""

        total_thinking_calls += int(parsed.get("thinking_calls") or 0)
        total_thinking_chars += int(parsed.get("thinking_chars") or 0)
        total_user_prompts += int(parsed.get("user_prompts") or 0)
        total_user_chars += int(parsed.get("user_chars") or 0)
        total_error_events += int(parsed.get("error_events") or 0)
        total_checkpoints += int(parsed.get("checkpoints") or 0)
        longest_flow_sec = max(longest_flow_sec, int(parsed.get("longest_flow_sec") or 0))
        total_flow_sec += int(parsed.get("total_flow_sec") or 0)
        max_single_out = max(max_single_out, int(parsed.get("max_single_out") or 0))
        max_single_in = max(max_single_in, int(parsed.get("max_single_in") or 0))
        max_conv_calls = max(max_conv_calls, int(parsed.get("conv_calls") or 0))
        max_conv_tools = max(max_conv_tools, sum(int(v) for v in (parsed.get("tools") or {}).values()))
        max_conv_files = max(max_conv_files, len(parsed.get("unique_files") or []))
        weekend_calls += int(parsed.get("weekend_calls") or 0)
        total_media_events += int(parsed.get("media_events") or 0)

        for ek, ev in (parsed.get("error_cats") or {}).items():
            error_cats_agg[ek] = error_cats_agg.get(ek, 0) + int(ev)

        for dk, op_row in (parsed.get("daily_ops") or {}).items():
            agg_op = daily_ops_agg.get(dk)
            if not agg_op:
                agg_op = [0, 0]
                daily_ops_agg[dk] = agg_op
            agg_op[0] += int(op_row[0])
            agg_op[1] += int(op_row[1])

        for t_name, t_cnt in (parsed.get("tools") or {}).items():
            tools_agg[t_name] = tools_agg.get(t_name, 0) + int(t_cnt)

        for srv_n, sc in (parsed.get("mcp_servers") or {}).items():
            mcp_servers_agg[srv_n] = mcp_servers_agg.get(srv_n, 0) + int(sc)

        for mt_k, mc in (parsed.get("mcp_tools") or {}).items():
            mcp_tools_agg[mt_k] = mcp_tools_agg.get(mt_k, 0) + int(mc)

        for cbin, cc in (parsed.get("cmd_bins") or {}).items():
            cmd_bins_agg[cbin] = cmd_bins_agg.get(cbin, 0) + int(cc)

        for ext_k, ext_cnt in (parsed.get("file_exts") or {}).items():
            ext_agg[ext_k] = ext_agg.get(ext_k, 0) + int(ext_cnt)

        for bn, frw in (parsed.get("file_rw") or {}).items():
            cur_f = file_rw_agg.get(bn)
            if not cur_f:
                file_rw_agg[bn] = [int(frw[0]), int(frw[1]), frw[2], frw[3]]
            else:
                cur_f[0] += int(frw[0])
                cur_f[1] += int(frw[1])
                if not cur_f[2] and frw[2]:
                    cur_f[2] = frw[2]

        for fp_k in (parsed.get("unique_files") or []):
            unique_files_agg.add(fp_k)

        for h in range(24):
            hourly_calls[h] += parsed["hourly"][h]
            hourly_fresh[h] += parsed["hourly_fresh"][h]

        pc = parsed.get("punchcard") or []
        for wd in range(min(7, len(pc))):
            row_pc = pc[wd]
            for hr in range(min(24, len(row_pc))):
                punchcard_agg[wd][hr] += int(row_pc[hr])

        for wk_k, wm_dict in (parsed.get("weekly_models") or {}).items():
            wk_agg = weekly_models_agg.get(wk_k)
            if not wk_agg:
                wk_agg = {}
                weekly_models_agg[wk_k] = wk_agg
            for mid, fr in wm_dict.items():
                canon_id, _, _ = _canonical_model(mid)
                wk_agg[canon_id] = wk_agg.get(canon_id, 0) + int(fr)

        conv_fresh = 0
        conv_out = 0
        conv_in = 0
        conv_ca = 0
        conv_wo_usd = 0.0
        conv_act_usd = 0.0
        conv_model_fresh = {}
        conv_model_calls = {}
        conv_model_names = {}
        for model_id, (c, inp, ca, out) in parsed["models"].items():
            canon_id, disp_name, effort = _canonical_model(model_id)
            conv_fresh += inp + out
            conv_out += out
            conv_in += inp
            conv_ca += ca
            wo_u, act_u, _ = _calc_model_usd(canon_id, inp, ca, out)
            conv_wo_usd += wo_u
            conv_act_usd += act_u
            conv_model_fresh[canon_id] = conv_model_fresh.get(canon_id, 0) + inp + out
            conv_model_calls[canon_id] = conv_model_calls.get(canon_id, 0) + c
            conv_model_names[canon_id] = disp_name

            m = models_agg.get(canon_id)
            if not m:
                m = {
                    "model": canon_id,
                    "displayName": disp_name,
                    "calls": 0,
                    "input": 0,
                    "cache": 0,
                    "output": 0,
                    "fresh": 0,
                    "total": 0,
                    "efforts": {},
                    "isOrchestrator": canon_id == "orchestrator-neon",
                    "isLegacyUnknown": False,
                }
                models_agg[canon_id] = m
            m["calls"] += c
            m["input"] += inp
            m["cache"] += ca
            m["output"] += out
            m["fresh"] += inp + out
            m["total"] += inp + ca + out
            if effort:
                m["efforts"][effort] = m["efforts"].get(effort, 0) + c

        c_max_ctx = int(parsed.get("max_ctx") or 0)
        if c_max_ctx > 0:
            conv_peak_ctxs.append(c_max_ctx)

        if not is_child and conv_fresh > 0:
            top_cm = max(conv_model_fresh.items(), key=lambda kv: kv[1])[0] if conv_model_fresh else "orchestrator-neon"
            c_calls = int(parsed.get("conv_calls") or 0)
            c_ckpts = int(parsed.get("checkpoints") or 0)
            c_errs = int(parsed.get("error_events") or 0)
            c_tools = parsed.get("tools") or {}
            c_exts = parsed.get("file_exts") or {}
            c_flow_min = round(int(parsed.get("total_flow_sec") or 0) / 60)
            c_saved_usd = round(max(0.0, conv_wo_usd - conv_act_usd), 2)
            c_est_usd = round(conv_act_usd, 2)
            arch_k = _classify_conv_archetype(conv_title, c_tools, c_exts, c_errs, c_calls, parent_children_agg.get(cid, 0))
            arch_lbl, arch_col, _ = ARCHETYPE_DEFS[arch_k]
            conv_rows.append(
                {
                    "id": cid,
                    "title": conv_title,
                    "workspace": ws_name or "默认工作区",
                    "calls": c_calls,
                    "fresh": conv_fresh,
                    "output": conv_out,
                    "topModel": top_cm,
                    "maxCtx": c_max_ctx,
                    "checkpoints": c_ckpts,
                    "archetype": arch_k,
                    "archetypeLabel": arch_lbl,
                    "archetypeColor": arch_col,
                    "estUsd": c_est_usd,
                    "savedUsd": c_saved_usd,
                    "flowMin": c_flow_min,
                }
            )
            c_prompt_t = conv_in + conv_ca
            c_tot_all = conv_in + conv_ca + conv_out
            conv_detail_raw[cid] = {
                "id": cid,
                "title": conv_title,
                "workspace": ws_name or "默认工作区",
                "firstDate": parsed.get("first_day") or "",
                "lastDate": parsed.get("last_day") or "",
                "calls": c_calls,
                "input": conv_in,
                "cache": conv_ca,
                "output": conv_out,
                "fresh": conv_fresh,
                "total": c_tot_all,
                "cacheHitRate": round((conv_ca / c_prompt_t) * 100, 1) if c_prompt_t > 0 else 0.0,
                "maxCtx": c_max_ctx,
                "checkpoints": c_ckpts,
                "flowMin": c_flow_min,
                "userPrompts": int(parsed.get("user_prompts") or 0),
                "toolsTotal": sum(int(v) for v in c_tools.values()),
                "errorEvents": c_errs,
                "filesCount": len(parsed.get("unique_files") or []),
                "archetype": arch_k,
                "archetypeLabel": arch_lbl,
                "archetypeColor": arch_col,
                "topModel": top_cm,
                "estUsd": c_est_usd,
                "savedUsd": c_saved_usd,
                "models": [
                    {
                        "model": mid,
                        "displayName": conv_model_names.get(mid, mid),
                        "fresh": mfr,
                        "calls": conv_model_calls.get(mid, 0),
                        "sharePct": round((mfr / max(1, conv_fresh)) * 100, 1),
                    }
                    for mid, mfr in sorted(conv_model_fresh.items(), key=lambda kv: -kv[1])[:5]
                ],
                "topTools": [
                    {
                        "name": tn,
                        "label": TOOL_LABELS.get(tn, tn),
                        "count": int(tc),
                    }
                    for tn, tc in sorted(c_tools.items(), key=lambda kv: -int(kv[1]))[:6]
                ],
            }
            ws_key = ws_name or "默认工作区"
            wrow = workspaces_agg.get(ws_key)
            if not wrow:
                wrow = {"name": ws_key, "convs": 0, "calls": 0, "fresh": 0, "output": 0}
                workspaces_agg[ws_key] = wrow
            wrow["convs"] += 1
            wrow["calls"] += c_calls
            wrow["fresh"] += conv_fresh
            wrow["output"] += conv_out

        for day_k, (c, inp, ca, out) in parsed["daily"].items():
            day = daily_agg.get(day_k)
            if not day:
                day = {
                    "date": day_k,
                    "calls": 0,
                    "input": 0,
                    "cache": 0,
                    "output": 0,
                    "fresh": 0,
                    "total": 0,
                    "convIds": set(),
                    "topConvs": [],
                }
                daily_agg[day_k] = day
            day["calls"] += c
            day["input"] += inp
            day["cache"] += ca
            day["output"] += out
            day["fresh"] += inp + out
            day["total"] += inp + ca + out
            if not is_child:
                day["convIds"].add(cid)
                day["topConvs"].append(
                    {
                        "id": cid,
                        "title": conv_title,
                        "calls": c,
                        "fresh": inp + out,
                        "output": out,
                    }
                )

    if cache_dirty:
        _save_disk_cache(data_dir, disk_cache)

    days_out = {}
    total_calls = 0
    total_input = 0
    total_cache = 0
    total_output = 0
    total_fresh = 0
    total_all = 0
    peak_day = None

    for day_k, d in daily_agg.items():
        top_convs = sorted(d["topConvs"], key=lambda x: -x["fresh"])[:5]
        row = {
            "date": day_k,
            "calls": d["calls"],
            "input": d["input"],
            "cache": d["cache"],
            "output": d["output"],
            "fresh": d["fresh"],
            "total": d["total"],
            "convs": len(d["convIds"]),
            "topConvs": top_convs,
        }
        days_out[day_k] = row
        total_calls += d["calls"]
        total_input += d["input"]
        total_cache += d["cache"]
        total_output += d["output"]
        total_fresh += d["fresh"]
        total_all += d["total"]
        if peak_day is None or row["fresh"] > peak_day["fresh"]:
            peak_day = row

    today_str = datetime.now().strftime("%Y-%m-%d")
    active_keys = sorted(days_out.keys())
    cur_streak, longest_streak = _compute_streaks(active_keys, today_str)
    chrono_icon, chrono_title, chrono_desc, peak_hr = _chronotype_label(hourly_calls)

    models_list = sorted(models_agg.values(), key=lambda x: -x["fresh"])
    for m in models_list:
        prompt_t = m["input"] + m["cache"]
        m["cacheHitRate"] = round((m["cache"] / prompt_t) * 100, 1) if prompt_t > 0 else 0.0
        m["sharePct"] = round((m["fresh"] / total_fresh) * 100, 1) if total_fresh > 0 else 0.0
        eff = sorted(m.get("efforts", {}).items(), key=lambda kv: -kv[1])
        m["effortSummary"] = " · ".join(f"{k} {v:,}次" for k, v in eff) if eff else ""
        if len(eff) > 1:
            m["effortBrief"] = f"{len(eff)}档思考"
        elif len(eff) == 1:
            m["effortBrief"] = eff[0][0]
        else:
            m["effortBrief"] = ""

    cmd_cnt = tools_agg.get("run_command", 0)
    read_cnt = tools_agg.get("read_file", 0) + tools_agg.get("view_file", 0) + tools_agg.get("view_file_outline", 0)
    write_cnt = (
        tools_agg.get("edit_file", 0)
        + tools_agg.get("write_file", 0)
        + tools_agg.get("write_to_file", 0)
        + tools_agg.get("replace_file_content", 0)
        + tools_agg.get("multi_replace_file_content", 0)
    )
    web_cnt = tools_agg.get("search_web", 0) + tools_agg.get("read_url_content", 0)
    browser_cnt = tools_agg.get("webview_browser", 0) + sum(v for k, v in tools_agg.items() if k.startswith("browser_"))
    total_tool_calls = sum(tools_agg.values())

    tools_summary = {
        "total": total_tool_calls,
        "commands": cmd_cnt,
        "reads": read_cnt,
        "writes": write_cnt,
        "webSearches": web_cnt,
        "browserOps": browser_cnt,
    }

    prompt_total = total_input + total_cache
    cache_hit_rate = round((total_cache / prompt_total) * 100, 1) if prompt_total > 0 else 0.0
    out_chars_est = int(total_output * 0.75)
    three_body_books = round(out_chars_est / 880000, 1)
    est_code_lines = round(total_output / 9)
    a4_pages = round(out_chars_est / 800)
    paper_height_m = round(a4_pages / 10000, 2)
    dev_years = round(est_code_lines / (150 * 365), 1)
    avg_daily_fresh = round(total_fresh / max(1, len(active_keys)))
    avg_daily_calls = round(total_calls / max(1, len(active_keys)))

    summary = {
        "activeDays": len(active_keys),
        "currentStreak": cur_streak,
        "longestStreak": longest_streak,
        "totalCalls": total_calls,
        "mainConvs": main_conv_count,
        "subConvs": sub_conv_count,
        "totalInput": total_input,
        "totalCache": total_cache,
        "totalOutput": total_output,
        "totalFresh": total_fresh,
        "totalProcessed": total_all,
        "cacheHitRate": cache_hit_rate,
        "avgDailyFresh": avg_daily_fresh,
        "avgDailyCalls": avg_daily_calls,
        "thinkingCalls": total_thinking_calls,
        "thinkingChars": total_thinking_chars,
        "threeBodyBooks": three_body_books,
        "estCodeLines": est_code_lines,
        "a4Pages": a4_pages,
        "paperHeightMeters": paper_height_m,
        "devYears": dev_years,
    }

    days_over_10m = sum(1 for r in days_out.values() if r["fresh"] >= 10_000_000)
    days_over_30m = sum(1 for r in days_out.values() if r["fresh"] >= 30_000_000)
    days_over_50m = sum(1 for r in days_out.values() if r["fresh"] >= 50_000_000)
    days_over_1m_out = sum(1 for r in days_out.values() if r["output"] >= 1_000_000)
    days_over_500_calls = sum(1 for r in days_out.values() if r["calls"] >= 500)
    days_over_1000_calls = sum(1 for r in days_out.values() if r["calls"] >= 1000)
    max_day_out_m = round(max((r["output"] for r in days_out.values()), default=0) / 1_000_000, 2)
    max_day_calls = max((r["calls"] for r in days_out.values()), default=0)
    active_months = len({dk[:7] for dk in days_out})
    new_file_writes = tools_agg.get("write_file", 0) + tools_agg.get("write_to_file", 0)
    edit_ops = (
        tools_agg.get("edit_file", 0)
        + tools_agg.get("replace_file_content", 0)
        + tools_agg.get("multi_replace_file_content", 0)
    )
    mcp_calls = (
        tools_agg.get("call_mcp_tool", 0)
        + tools_agg.get("list_resources", 0)
        + tools_agg.get("read_resource", 0)
    )
    task_ops = tools_agg.get("manage_task", 0) + tools_agg.get("schedule", 0)
    workflow_ops = tools_agg.get("run_workflow", 0) + tools_agg.get("schedule", 0)
    named_models = sum(1 for m in models_list if not m["isLegacyUnknown"])
    agent_msgs = (
        tools_agg.get("invoke_subagent", 0)
        + tools_agg.get("send_message", 0)
        + tools_agg.get("manage_subagents", 0)
        + tools_agg.get("run_workflow", 0)
    )
    plan_ops = (
        tools_agg.get("enter_plan_mode", 0)
        + tools_agg.get("exit_plan_mode", 0)
        + tools_agg.get("update_steps", 0)
        + tools_agg.get("update_focus", 0)
    )
    img_gen_cnt = tools_agg.get("generate_image", 0)

    # Today rings
    today_row = days_out.get(today_str) or {}
    today_fresh = int(today_row.get("fresh") or 0)
    today_calls = int(today_row.get("calls") or 0)
    today_ring = {
        "date": today_str,
        "fresh": today_fresh,
        "calls": today_calls,
        "freshPct": min(999, round((today_fresh / max(1, avg_daily_fresh)) * 100)),
        "callsPct": min(999, round((today_calls / max(1, avg_daily_calls)) * 100)),
        "streakActive": today_str in days_out,
    }

    # 6-Axis Geek Capability Radar (0..100 calibrated against high power-user benchmarks)
    def _norm(val, bench):
        return min(99, max(12, round((max(0, val) / max(1, bench)) ** 0.52 * 100)))

    code_val = write_cnt + read_cnt
    think_wan = round(total_thinking_chars / 10000)
    scout_val = web_cnt + browser_cnt
    swarm_val = sub_conv_count * 12 + agent_msgs
    endur_val = longest_streak * 220 + max_conv_calls

    radar_stats = [
        {
            "key": "code",
            "label": "代码铸造",
            "score": _norm(code_val, 36000),
            "raw": code_val,
            "desc": "源码编辑写入与工程阅读检视的综合编码强度",
            "detail": f"改写 {write_cnt:,} + 阅读 {read_cnt:,} = {code_val:,} 次",
            "formula": "round((代码读写总数 / 36,000)^0.52 × 100)，上限 99",
        },
        {
            "key": "think",
            "label": "深度推演",
            "score": _norm(think_wan, 3200),
            "raw": think_wan,
            "desc": "驱动 AI 进行长链深度思考与架构推演的总字量",
            "detail": f"深度思考 {think_wan:,} 万字（共触发 {total_thinking_calls:,} 次）",
            "formula": "round((深度思考万字数 / 3,200)^0.52 × 100)，上限 99",
        },
        {
            "key": "cmd",
            "label": "终端执行",
            "score": _norm(cmd_cnt, 45000),
            "raw": cmd_cnt,
            "desc": "命令行编译、测试、脚本与自动化运维驱动强度",
            "detail": f"累计执行终端命令 {cmd_cnt:,} 次",
            "formula": "round((终端命令次数 / 45,000)^0.52 × 100)，上限 99",
        },
        {
            "key": "scout",
            "label": "情报侦察",
            "score": _norm(scout_val, 6500),
            "raw": scout_val,
            "desc": "实时联网检索、资料核验与内置浏览器真机实操",
            "detail": f"网页检索 {web_cnt:,} + 浏览器实操 {browser_cnt:,} = {scout_val:,} 次",
            "formula": "round((检索+浏览器操作 / 6,500)^0.52 × 100)，上限 99",
        },
        {
            "key": "swarm",
            "label": "集群统帅",
            "score": _norm(swarm_val, 2200),
            "raw": sub_conv_count,
            "desc": "多代理并发派发与跨代理消息协同指挥能力",
            "detail": f"子代理 {sub_conv_count}×12 + 调度消息 {agent_msgs:,} = {swarm_val:,} 点",
            "formula": "round(((子代理×12 + 调度消息) / 2,200)^0.52 × 100)，上限 99",
        },
        {
            "key": "endurance",
            "label": "持久耐力",
            "score": _norm(endur_val, 15000),
            "raw": longest_streak,
            "desc": "连续作战毅力与单场超长深度交互马拉松实力",
            "detail": f"最长连击 {longest_streak}天×220 + 单场最高 {max_conv_calls:,}轮 = {endur_val:,} 点",
            "formula": "round(((最长连击×220 + 单场最高轮) / 15,000)^0.52 × 100)，上限 99",
        },
    ]

    # Language DNA breakdown (pure code/config/markup languages only; exclude binary/image/log assets)
    lang_map = {}
    for ext_k, cnt in ext_agg.items():
        if ext_k not in EXT_LANG_MAP:
            continue
        lname, lcol = EXT_LANG_MAP[ext_k]
        row_l = lang_map.get(lname)
        if not row_l:
            row_l = {"name": lname, "color": lcol, "count": 0}
            lang_map[lname] = row_l
        row_l["count"] += cnt
    lang_sorted = sorted(lang_map.values(), key=lambda x: -x["count"])
    total_lang_ops = max(1, sum(x["count"] for x in lang_sorted))
    languages_out = []
    for x in lang_sorted[:7]:
        languages_out.append({**x, "pct": round((x["count"] / total_lang_ops) * 100, 1)})
    if len(lang_sorted) > 7:
        other_cnt = sum(x["count"] for x in lang_sorted[7:])
        languages_out.append({"name": "其他", "color": "#64748b", "count": other_cnt, "pct": round((other_cnt / total_lang_ops) * 100, 1)})

    # Top Workspaces & Top Epic Conversations (top 20 so UI can switch sort tabs!) & Swarm Parents
    workspaces_out = sorted(workspaces_agg.values(), key=lambda x: -x["fresh"])[:8]
    top_convs_out = sorted(conv_rows, key=lambda x: -x["fresh"])[:20]

    top_parents = []
    for p_cid, child_cnt in sorted(parent_children_agg.items(), key=lambda kv: -kv[1])[:5]:
        p_match = next((r for r in conv_rows if r["id"] == p_cid), None)
        top_parents.append({
            "title": p_match["title"] if p_match else (titles.get(p_cid) or f"任务 {p_cid[:6]}"),
            "children": child_cnt,
            "fresh": p_match["fresh"] if p_match else 0,
        })
    swarm_stats = {
        "parents": len(parent_children_agg),
        "subs": sub_conv_count,
        "maxChildren": max(parent_children_agg.values()) if parent_children_agg else 0,
        "maxDepth": max(1, max_nesting) if sub_conv_count else 0,
        "topParents": top_parents,
    }

    # Weekly Model Evolution (last 12 active ISO weeks)
    weekly_models_out = []
    for wk_k in sorted(weekly_models_agg.keys())[-12:]:
        wm = weekly_models_agg[wk_k]
        tot_w = sum(wm.values())
        segs = [{"model": mid, "fresh": fr} for mid, fr in sorted(wm.items(), key=lambda kv: -kv[1])]
        try:
            mon_dt = datetime.strptime(wk_k + "-1", "%G-W%V-%u")
            wk_lbl = mon_dt.strftime("%m/%d")
        except Exception:
            wk_lbl = wk_k[-3:]
        weekly_models_out.append({"week": wk_lbl, "iso": wk_k, "totalFresh": tot_w, "segments": segs})

    # Model-Tiered FinOps Attribution & Cache Savings
    finops_by_model = []
    tot_wo = 0.0
    tot_act = 0.0
    for m in models_list:
        r_in, r_ca, r_out = MODEL_PRICING_PER_M.get(m["model"], DEFAULT_PRICING_PER_M)
        wo_u, act_u, sav_u = _calc_model_usd(m["model"], m["input"], m["cache"], m["output"])
        tot_wo += wo_u
        tot_act += act_u
        finops_by_model.append({
            "model": m["model"],
            "displayName": m["displayName"],
            "withoutCacheUsd": round(wo_u, 1),
            "actualUsd": round(act_u, 1),
            "savedUsd": round(sav_u, 1),
            "savedPct": round((sav_u / max(0.01, wo_u)) * 100, 1) if wo_u > 0 else 0.0,
            "rates": {"in": r_in, "cache": r_ca, "out": r_out},
        })
    finops_by_model.sort(key=lambda x: -x["withoutCacheUsd"])
    tot_act_safe = max(0.1, tot_act)
    for fm in finops_by_model:
        fm["sharePct"] = round((fm["actualUsd"] / tot_act_safe) * 100, 1)

    tot_saved = max(0.0, tot_wo - tot_act)
    active_cnt = max(1, len(active_keys))
    avg_daily_usd = round(tot_act / active_cnt, 1)
    finops_out = {
        "totalWithoutCacheUsd": round(tot_wo, 1),
        "totalActualUsd": round(tot_act, 1),
        "totalSavedUsd": round(tot_saved, 1),
        "savedPct": round((tot_saved / max(0.1, tot_wo)) * 100, 1),
        "avgDailyUsd": avg_daily_usd,
        "proj30dUsd": round(avg_daily_usd * 30, 1),
        "byModel": finops_by_model[:8],
    }
    api_estimate = {
        "withoutCacheUsd": finops_out["totalWithoutCacheUsd"],
        "actualUsd": finops_out["totalActualUsd"],
        "savedUsd": finops_out["totalSavedUsd"],
        "savedPct": finops_out["savedPct"],
    }

    # Conversation Archetypes (6-way deterministic clustering)
    arch_map = {
        k: {"key": k, "label": v[0], "color": v[1], "rule": v[2], "convs": 0, "calls": 0, "fresh": 0}
        for k, v in ARCHETYPE_DEFS.items()
    }
    for r in conv_rows:
        ak = r.get("archetype") or "feature_build"
        ar = arch_map.get(ak)
        if ar:
            ar["convs"] += 1
            ar["calls"] += r["calls"]
            ar["fresh"] += r["fresh"]
    arch_fresh_tot = max(1, sum(x["fresh"] for x in arch_map.values()))
    archetypes_out = []
    for x in sorted(arch_map.values(), key=lambda a: -a["fresh"]):
        archetypes_out.append({**x, "pct": round((x["fresh"] / arch_fresh_tot) * 100, 1)})

    # Context Window Health & Error Resilience
    ctx_sorted = sorted(conv_peak_ctxs)
    ctx_n = max(1, len(ctx_sorted))
    b_counts = [
        ("lt64k", "<64K", sum(1 for v in ctx_sorted if v < 64_000)),
        ("b64_200k", "64K–200K", sum(1 for v in ctx_sorted if 64_000 <= v < 200_000)),
        ("b200_500k", "200K–500K", sum(1 for v in ctx_sorted if 200_000 <= v < 500_000)),
        ("gte500k", "500K+", sum(1 for v in ctx_sorted if v >= 500_000)),
    ]
    err_meta = [
        ("api_net", "上游 API / 流中断"),
        ("edit_match", "代码定位未命中"),
        ("file_missing", "路径缺失 / 未找到"),
        ("other", "其他工具异常"),
    ]
    err_sum = max(1, sum(error_cats_agg.values()))
    top_ctx_list = [
        {
            "id": r["id"],
            "title": r["title"],
            "workspace": r["workspace"],
            "maxCtx": r["maxCtx"],
            "checkpoints": r["checkpoints"],
            "fresh": r["fresh"],
            "calls": r["calls"],
            "topModel": r["topModel"],
        }
        for r in sorted(conv_rows, key=lambda x: (-x.get("maxCtx", 0), -x["fresh"]))[:5]
        if r.get("maxCtx", 0) > 0
    ]
    context_health_out = {
        "maxCtx": ctx_sorted[-1] if ctx_sorted else 0,
        "avgPeakCtx": round(sum(ctx_sorted) / ctx_n) if ctx_sorted else 0,
        "p90PeakCtx": ctx_sorted[min(len(ctx_sorted) - 1, int(len(ctx_sorted) * 0.9))] if ctx_sorted else 0,
        "totalCheckpoints": total_checkpoints,
        "heavyConvsCount": b_counts[3][2],
        "totalNodes": len(ctx_sorted),
        "buckets": [
            {"key": k, "label": lbl, "count": cnt, "pct": round((cnt / ctx_n) * 100, 1)}
            for k, lbl, cnt in b_counts
        ],
        "errorTotal": sum(error_cats_agg.values()),
        "errorCats": [
            {"key": k, "label": lbl, "count": error_cats_agg.get(k, 0), "pct": round((error_cats_agg.get(k, 0) / err_sum) * 100, 1)}
            for k, lbl in err_meta
        ],
        "topConvs": top_ctx_list,
    }

    # 7-Day Rolling Momentum vs Previous 7 Calendar Days
    anchor_str = active_keys[-1] if active_keys else today_str
    anchor_dt = datetime.strptime(anchor_str, "%Y-%m-%d").date()
    c7_fresh = c7_out = c7_calls = c7_tools = c7_think = c7_days = 0
    p7_fresh = p7_out = p7_calls = p7_tools = p7_think = p7_days = 0
    for i in range(7):
        dk_c = (anchor_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        row_c = days_out.get(dk_c)
        op_c = daily_ops_agg.get(dk_c)
        if row_c:
            c7_days += 1
            c7_fresh += row_c["fresh"]
            c7_out += row_c["output"]
            c7_calls += row_c["calls"]
        if op_c:
            c7_tools += op_c[0]
            c7_think += op_c[1]

        dk_p = (anchor_dt - timedelta(days=7 + i)).strftime("%Y-%m-%d")
        row_p = days_out.get(dk_p)
        op_p = daily_ops_agg.get(dk_p)
        if row_p:
            p7_days += 1
            p7_fresh += row_p["fresh"]
            p7_out += row_p["output"]
            p7_calls += row_p["calls"]
        if op_p:
            p7_tools += op_p[0]
            p7_think += op_p[1]

    momentum_out = {
        "cur7Fresh": c7_fresh,
        "prev7Fresh": p7_fresh,
        "freshDeltaPct": _pct_delta(c7_fresh, p7_fresh),
        "cur7Output": c7_out,
        "prev7Output": p7_out,
        "outputDeltaPct": _pct_delta(c7_out, p7_out),
        "cur7Calls": c7_calls,
        "prev7Calls": p7_calls,
        "callsDeltaPct": _pct_delta(c7_calls, p7_calls),
        "cur7Tools": c7_tools,
        "prev7Tools": p7_tools,
        "toolsDeltaPct": _pct_delta(c7_tools, p7_tools),
        "cur7Thinking": c7_think,
        "prev7Thinking": p7_think,
        "thinkingDeltaPct": _pct_delta(c7_think, p7_think),
        "cur7ActiveDays": c7_days,
        "prev7ActiveDays": p7_days,
    }

    # Tool Breakdown, MCP Ecosystem, Shell Stack & Hot Files
    tot_t_safe = max(1, total_tool_calls)
    tool_breakdown_out = [
        {
            "name": nm,
            "label": TOOL_LABELS.get(nm, nm),
            "count": cnt,
            "pct": round((cnt / tot_t_safe) * 100, 1),
        }
        for nm, cnt in sorted(tools_agg.items(), key=lambda kv: -kv[1])[:10]
    ]

    tot_mcp_safe = max(1, sum(mcp_servers_agg.values()))
    mcp_eco_out = []
    for srv_n, sc in sorted(mcp_servers_agg.items(), key=lambda kv: -kv[1])[:10]:
        sub_ts = [
            {"name": k.split("/", 1)[1], "calls": v}
            for k, v in sorted(mcp_tools_agg.items(), key=lambda kv: -kv[1])
            if k.startswith(srv_n + "/")
        ][:3]
        mcp_eco_out.append({
            "server": srv_n,
            "calls": sc,
            "pct": round((sc / tot_mcp_safe) * 100, 1),
            "topTools": sub_ts,
        })

    tot_cmd_safe = max(1, sum(cmd_bins_agg.values()))
    shell_stack_out = [
        {"cmd": k, "calls": v, "pct": round((v / tot_cmd_safe) * 100, 1)}
        for k, v in sorted(cmd_bins_agg.items(), key=lambda kv: -kv[1])[:10]
    ]

    hot_files_out = []
    for bn, frw in sorted(file_rw_agg.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:10]:
        r_c, w_c, ext_v, s_path = frw
        lname, lcol = EXT_LANG_MAP.get(ext_v, (ext_v.lstrip(".").upper() or "File", "#64748b"))
        hot_files_out.append({
            "name": bn,
            "ext": ext_v,
            "lang": lname,
            "color": lcol,
            "reads": r_c,
            "writes": w_c,
            "total": r_c + w_c,
            "samplePath": s_path,
        })

    # Data-Backed Automated Insights (4 Diagnostic Cards)
    fd = momentum_out["freshDeltaPct"]
    if fd is None:
        mom_metric = f"{c7_days} 天/周"
        mom_body = f"近 7 天累计净耗 {round(c7_fresh / 1e6, 1)}M Tokens、生成 {round(c7_out / 1e6, 1)}M 输出；当前连续 {cur_streak} 天，距历史纪录 {longest_streak} 天仅差 {max(0, longest_streak - cur_streak)} 天。"
        mom_tone = "green"
    else:
        sign = "+" if fd >= 0 else ""
        mom_metric = f"{sign}{fd}%"
        gap_d = max(0, longest_streak - cur_streak)
        gap_txt = "已追平历史最长连击！" if gap_d == 0 and cur_streak > 0 else f"距 {longest_streak} 天纪录差 {gap_d} 天"
        mom_body = f"近 7 日算力较前 7 日 {sign}{fd}%（{round(c7_fresh / 1e6, 1)}M vs {round(p7_fresh / 1e6, 1)}M），当前连击 {cur_streak} 天（{gap_txt}）。"
        mom_tone = "green" if fd >= 0 else "amber"

    top_tool_row = tool_breakdown_out[0] if tool_breakdown_out else {"label": "工具", "pct": 0}
    top_err_row = max(context_health_out["errorCats"], key=lambda x: x["count"]) if total_error_events > 0 else {"label": "无异常", "count": 0}
    top2_share = round(sum(m["sharePct"] for m in models_list[:2]), 1)
    top_m_name = models_list[0]["displayName"] if models_list else "AI"

    insights_out = [
        {
            "id": "momentum",
            "tag": "节律动量",
            "tone": mom_tone,
            "title": "7 日算力趋势与连击状态",
            "metric": mom_metric,
            "body": mom_body,
        },
        {
            "id": "context",
            "tag": "上下文健康",
            "tone": "sky",
            "title": "大窗吞吐与自动压缩负荷",
            "metric": f"{total_checkpoints} 次压缩",
            "body": f"单场上下文峰值达 {round(context_health_out['maxCtx'] / 1000):,}K Tokens（均值 {round(context_health_out['avgPeakCtx'] / 1000):,}K），共有 {context_health_out['heavyConvsCount']} 场迈入 500K+ 重型工程。",
        },
        {
            "id": "autonomy",
            "tag": "代理效能",
            "tone": "purple",
            "title": "工具自动化与排错自愈",
            "metric": f"{round(total_tool_calls / max(1, total_user_prompts), 1)} 工具/问",
            "body": f"{top_tool_row['label']} 占比 {top_tool_row['pct']}% 位居第一；累计自愈 {total_error_events:,} 次异常，首要诱因为「{top_err_row['label']}」（{top_err_row['count']} 次）。",
        },
        {
            "id": "finops",
            "tag": "FinOps 账单",
            "tone": "gold",
            "title": "分模型阶梯缓存省费报告",
            "metric": f"省 ${finops_out['totalSavedUsd']:,.0f}",
            "body": f"按模型公开 API 定价加权折算，Prompt 缓存命中省下 {finops_out['savedPct']}%（日均实际 ${finops_out['avgDailyUsd']}）；{top_m_name} 等前两款主力占 {top2_share}% 算力。",
        },
    ]

    # Select conversation details to serialize (keep payload fast & complete for every visible card)
    detail_ids = set()
    for r in sorted(conv_rows, key=lambda x: -x["fresh"])[:60]:
        detail_ids.add(r["id"])
    for r in sorted(conv_rows, key=lambda x: -x["calls"])[:15]:
        detail_ids.add(r["id"])
    for r in sorted(conv_rows, key=lambda x: -x.get("maxCtx", 0))[:15]:
        detail_ids.add(r["id"])
    for drow in days_out.values():
        for tc in drow.get("topConvs") or []:
            if tc.get("id"):
                detail_ids.add(tc["id"])
    conv_details_out = {cid: conv_detail_raw[cid] for cid in detail_ids if cid in conv_detail_raw}

    leverage_stats = {
        "userPrompts": total_user_prompts,
        "userChars": total_user_chars,
        "outChars": out_chars_est,
        "charRatio": round(out_chars_est / max(1, total_user_chars), 1) if total_user_chars > 0 else 0.0,
        "toolsPerPrompt": round(total_tool_calls / max(1, total_user_prompts), 1) if total_user_prompts > 0 else 0.0,
        "errorRecoveries": total_error_events,
        "longestFlowHours": round(longest_flow_sec / 3600, 1),
        "totalFlowHours": round(total_flow_sec / 3600, 1),
    }

    extra_stats = {
        "userPrompts": total_user_prompts,
        "userCharsK": round(total_user_chars / 1000, 1),
        "uniqueFiles": len(unique_files_agg),
        "epicConvs": sum(1 for r in conv_rows if r["calls"] >= 300),
        "megaConvs": sum(1 for r in conv_rows if r["calls"] >= 1000),
        "newFileWrites": new_file_writes,
        "editOps": edit_ops,
        "maxConvFiles": max_conv_files,
        "maxConvTools": max_conv_tools,
        "activeWeeks": len(weekly_models_agg),
        "activeMonths": active_months,
        "maxWeekFreshM": round(max((sum(wm.values()) for wm in weekly_models_agg.values()), default=0) / 1_000_000, 1),
        "daysOver500Calls": days_over_500_calls,
        "daysOver1000Calls": days_over_1000_calls,
        "maxSingleInK": round(max_single_in / 1000, 1),
        "errorRecoveries": total_error_events,
        "totalFlowHours": round(total_flow_sec / 3600, 1),
        "longestFlowHours": round(longest_flow_sec / 3600, 1),
        "maxSwarmChildren": swarm_stats["maxChildren"],
        "maxConvFreshM": round(max((r["fresh"] for r in conv_rows), default=0) / 1_000_000, 1),
        "maxConvCalls": max_conv_calls,
        "maxDayOutM": max_day_out_m,
        "maxDayCalls": max_day_calls,
        "weekendCalls": weekend_calls,
        "mediaEvents": total_media_events + img_gen_cnt,
        "imgGenCnt": img_gen_cnt,
        "mcpCalls": mcp_calls,
        "taskOps": task_ops,
        "workflowOps": workflow_ops,
        "daysOver10M": days_over_10m,
        "daysOver30M": days_over_30m,
        "daysOver50M": days_over_50m,
        "daysOver1MOut": days_over_1m_out,
        "agentMsgs": agent_msgs,
        "planOps": plan_ops,
    }
    achievements = _build_achievements(summary, peak_day, tools_summary, hourly_calls, extra_stats)

    result = {
        "today": today_str,
        "firstDate": active_keys[0] if active_keys else today_str,
        "lastDate": active_keys[-1] if active_keys else today_str,
        "prefs": load_ui_prefs(data_dir),
        "pluginRepo": _load_plugin_repo(),
        "summary": summary,
        "tools": tools_summary,
        "achievements": achievements,
        "peakDay": peak_day,
        "chronotype": {
            "icon": chrono_icon,
            "title": chrono_title,
            "desc": chrono_desc,
            "peakHr": peak_hr,
            "hourlyCalls": hourly_calls,
            "hourlyFresh": hourly_fresh,
        },
        "models": models_list,
        "days": days_out,
        "todayRing": today_ring,
        "radarStats": radar_stats,
        "leverage": leverage_stats,
        "apiEstimate": api_estimate,
        "punchcard": punchcard_agg,
        "weeklyModels": weekly_models_out,
        "languages": languages_out,
        "workspaces": workspaces_out,
        "topConvs": top_convs_out,
        "swarm": swarm_stats,
        "finops": finops_out,
        "archetypes": archetypes_out,
        "contextHealth": context_health_out,
        "momentum": momentum_out,
        "toolBreakdown": tool_breakdown_out,
        "mcpEcosystem": mcp_eco_out,
        "shellStack": shell_stack_out,
        "hotFiles": hot_files_out,
        "insights": insights_out,
        "convDetails": conv_details_out,
    }
    _MEM_CACHE["result_sig"] = sig
    _MEM_CACHE["result_data"] = result
    return result


if __name__ == "__main__":
    res = collect_heatmap_data()
    print(json.dumps({k: v for k, v in res.items() if k != "days"}, ensure_ascii=False, indent=2))
