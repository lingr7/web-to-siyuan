#!/usr/bin/env python3
"""
zhihu_reclip.py — 思源笔记知乎剪藏失败笔记 修复工具（千条规模）

架构核心：状态机 + 断点续传 + 原子写入
  任何时刻中断，重新运行即可从断点恢复，不丢进度、不重复执行。

状态文件 reclip_state.json 记录每个 URL 的独立处理进度：
  pending → deleted → fetched → created → verified → done
  任何阶段失败标记为 failed_X，可单独重试。

阶段（每阶段幂等，可重跑）：
  scan     扫描思源，识别失败笔记，初始化状态
  delete   删除旧的失败文档
  prepare  导出待抓取 URL 清单（给 WorkBuddy 用）
  process  读取 WorkBuddy 抓取好的内容，创建新文档
  verify   验证所有新文档标题正确
  report   生成最终报告
  status   查看当前进度（只读）
  reset    重置指定 URL 的状态（重试）

用法示例见 --help 或 README
"""

import json
import os
import re
import sys
import hashlib
import argparse
import urllib.request
import urllib.error
import time
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

# ============================================================
#  配置
# ============================================================

SIYUAN_API = os.environ.get("SIYUAN_API", "http://127.0.0.1:6806")
DEFAULT_NOTEBOOK = None  # 运行时自动检测
# 目标笔记本名（剪藏失败笔记在印象笔记迁移后所在笔记本），可用环境变量覆盖
NOTEBOOK_NAME = os.environ.get("SIYUAN_NOTEBOOK_NAME", "印象笔记")

# 运行数据目录与代码分离（脚本已并入 zhihu-to-siyuan skill 仓，
# 状态/缓存/日志等运行数据不能写进 skill 目录）
WORK_DIR = Path(os.environ.get("RECLIP_DATA_DIR", r"D:\zhihu-reclip-data")).resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = WORK_DIR / "reclip_state.json"
CONTENT_CACHE_DIR = WORK_DIR / "content_cache"
FETCH_QUEUE_FILE = WORK_DIR / "fetch_queue.jsonl"      # 待抓取 URL（每行一个 JSON）
FETCH_INPUT_DIR = WORK_DIR / "fetched_content"          # WorkBuddy 抓取结果放入这里
LOG_FILE = WORK_DIR / "reclip.log"

# 限流：思源 API 每次操作之间的最小间隔（秒）
API_INTERVAL = 0.15
# 最大重试次数
MAX_RETRIES = 3
# 重试退避基数（秒）
RETRY_BACKOFF = 2.0

FULLWIDTH_SLASH = '\uff0f'
ZHIHU_URL_PATTERN = re.compile(
    r'https?://(?:www\.|zhuanlan\.|link\.)?(?:fx)?zhihu\.com/[^\s\)\]\}】）\u3000\n\r]*',
    re.IGNORECASE
)

# ============================================================
#  Playwright 抓取链路（来自 zhihu-to-siyuan skill 的经验）
#  WebFetch 抓知乎：图片全丢、长回答截断、多回答页抓错 → 不可用于批量重建
#  正确链路：zhihu_extract.js 用本机 Chrome 无头抓取，产出 blocks+图片
# ============================================================

NODE_WORKSPACE = r"C:\Users\redmi\.workbuddy\binaries\node\workspace"
NODE_EXE = r"C:\Users\redmi\.workbuddy\binaries\node\versions\22.22.2\node.exe"
# zhihu_extract.js 与本脚本同目录（同在 skill 的 scripts/ 下），
# 可用环境变量覆盖
ZHIHU_EXTRACT_JS = os.environ.get(
    "ZHIHU_EXTRACT_JS",
    str(Path(__file__).parent / "zhihu_extract.js"),
)
ARTICLES_FILE = WORK_DIR / "articles.json"        # zhihu_extract.js 的输入清单
EXTRACT_DIR = WORK_DIR / "zhihu_extract"          # 抓取产出目录
PW_BROWSERS_PATH = os.path.join(NODE_WORKSPACE, "pw-browsers")

# 知乎页面垃圾块（"xx 赞同"等元信息，来自 siyuan_pipeline.py）
JUNK_PATTERNS = [
    re.compile(r"\d+\s*赞同"),
    re.compile(r"^发布于"),
    re.compile(r"^编辑于"),
    re.compile(r"^\d+\s*人赞同了该回答"),
]


def detect_url_type(url):
    """
    知乎 URL 类型识别（zhihu_extract.js 需要 type 字段）：
      answer  /question/<qid>/answer/<aid>
      pin     /pin/<id>
      article zhuanlan.zhihu.com/p/<id> 或 /p/<id>
    """
    u = normalize_zhihu_url(url)
    if re.search(r'/question/\d+/answer/\d+', u):
        return 'answer'
    if '/pin/' in u:
        return 'pin'
    if re.search(r'(/p/\d+|zhuanlan\.zhihu\.com/p/)', u):
        return 'article'
    if '/question/' in u:
        return 'question'   # 问题页（整页），extract.js 可能不支持，标记待人工
    return 'unknown'


def is_junk_text(text):
    """知乎页面垃圾块过滤（来自 siyuan_pipeline.py 实战验证）"""
    return any(p.search(text) for p in JUNK_PATTERNS)


# ============================================================
#  工具函数
# ============================================================

def log(msg, level="INFO"):
    """同时输出到控制台和日志文件"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def normalize_text(text):
    """全角斜杠 → 半角"""
    if not text:
        return text
    return text.replace(FULLWIDTH_SLASH, '/')


def extract_zhihu_urls(text):
    """提取所有知乎链接，去重，清理尾部标点"""
    if not text:
        return []
    normalized = normalize_text(text)
    urls = ZHIHU_URL_PATTERN.findall(normalized)
    cleaned, seen = [], set()
    for url in urls:
        url = url.rstrip('.,;:!?。，；：！？]')
        if url not in seen:
            seen.add(url)
            cleaned.append(url)
    return cleaned


def normalize_zhihu_url(url):
    """归一化：fxzhihu→zhihu，去跟踪参数"""
    url = normalize_text(url)
    url = re.sub(r'(fxzhihu\.com)', 'zhihu.com', url)
    url = url.split('?')[0]
    # 去除尾部斜杠
    url = url.rstrip('/')
    return url


def url_hash(url):
    """URL → 短哈希，用于文件命名"""
    return hashlib.md5(url.encode('utf-8')).hexdigest()[:12]


def atomic_write_json(path, data):
    """
    原子写入 JSON：先写临时文件，再 rename。
    保证状态文件永远不会因为中途崩溃而损坏。
    """
    path = Path(path)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix='.tmp', prefix=path.stem
    )
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Windows 下 rename 目标已存在会失败，用 replace
        os.replace(tmp_path, str(path))
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# ============================================================
#  思源 API 封装（含重试 + 限流）
# ============================================================

_last_api_call = 0.0


def siyuan_request(endpoint, payload=None, retries=None):
    """调用思源 API，自动限流 + 指数退避重试"""
    global _last_api_call
    if retries is None:
        retries = MAX_RETRIES

    # 限流
    elapsed = time.time() - _last_api_call
    if elapsed < API_INTERVAL:
        time.sleep(API_INTERVAL - elapsed)

    url = f"{SIYUAN_API}{endpoint}"
    data = json.dumps(payload or {}).encode('utf-8')

    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data, headers={'Content-Type': 'application/json'}
            )
            resp = urllib.request.urlopen(req, timeout=60)
            _last_api_call = time.time()
            result = json.loads(resp.read().decode('utf-8'))
            return result
        except urllib.error.HTTPError as e:
            _last_api_call = time.time()
            last_error = f"HTTP {e.code}: {e.reason}"
            # 4xx 不重试（参数错误）
            if 400 <= e.code < 500:
                return {"code": -1, "msg": f"HTTP {e.code}: {e.reason}"}
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            _last_api_call = time.time()
            last_error = str(e)
        except Exception as e:
            _last_api_call = time.time()
            last_error = f"{type(e).__name__}: {e}"

        if attempt < retries - 1:
            wait = RETRY_BACKOFF * (2 ** attempt)
            log(f"  API 重试 {attempt + 1}/{retries} ({endpoint}): {last_error}, 等待 {wait:.0f}s", "WARN")
            time.sleep(wait)

    log(f"  API 失败 ({endpoint}): {last_error} (已重试 {retries} 次)", "ERROR")
    return {"code": -1, "msg": last_error}


def check_api():
    """验证 API 可用并返回 notebook_id（优先匹配 NOTEBOOK_NAME）"""
    result = siyuan_request('/api/system/version')
    if result.get('code') != 0:
        log(f"思源 API 不可用: {result.get('msg', '')}", "ERROR")
        return None
    log(f"思源 API 正常 (v{result['data']})")
    nb_id = ensure_notebook_id(None)
    if not nb_id:
        log("没有可用笔记本", "ERROR")
    return nb_id


def ensure_notebook_id(state=None):
    """
    校验/刷新 notebook_id（千条规模的防过期经验：笔记本可能被删除/关闭，
    状态文件里存的 ID 会失效导致 createDocWithMd 报"查询笔记本失败"）。
    优先级: 状态里仍有效的 ID > 名称匹配(印象笔记) > 第一个打开的笔记本。
    """
    result = siyuan_request('/api/notebook/lsNotebooks')
    if result.get('code') != 0:
        log(f"获取笔记本列表失败: {result.get('msg', '')}", "ERROR")
        return None
    notebooks = result.get('data', {}).get('notebooks', [])
    open_nbs = [nb for nb in notebooks if not nb.get('closed', True)]

    # 1) 状态里存的 ID 仍有效
    if state and state.get('notebook_id'):
        for nb in open_nbs:
            if nb['id'] == state['notebook_id']:
                return state['notebook_id']

    # 2) 名称匹配
    for nb in open_nbs:
        if nb['name'] == NOTEBOOK_NAME:
            if state is not None and state.get('notebook_id') != nb['id']:
                log(f"notebook_id 已刷新: {nb['name']} ({nb['id']})", "WARN")
                state['notebook_id'] = nb['id']
            return nb['id']

    # 3) 第一个打开的笔记本
    if open_nbs:
        nb = open_nbs[0]
        if state is not None:
            state['notebook_id'] = nb['id']
        log(f"未找到笔记本「{NOTEBOOK_NAME}」，使用 {nb['name']} ({nb['id']})", "WARN")
        return nb['id']
    return None


# ============================================================
#  图片资产处理（来自 zhihu-to-siyuan skill 实战经验）
#  坑1: /api/asset/upload 文件字段名是 "file[]"，不是 files[]
#  坑2: 上传落在全局 /data/assets/，文档内 assets/ 相对链接按笔记本目录解析
#        → 必须 getFile 读出再 putFile 到 /data/<notebook>/assets/
# ============================================================

import mimetypes
import uuid


def upload_asset(filepath, rename_prefix):
    """上传图片到思源（落在全局 /data/assets/，之后由 fix_assets 归位）"""
    filename = f"zhihu_{rename_prefix}_{os.path.basename(filepath)}"
    mime = mimetypes.guess_type(filepath)[0] or "image/jpeg"
    boundary = uuid.uuid4().hex
    with open(filepath, 'rb') as f:
        filebytes = f.read()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file[]"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        filebytes,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"{SIYUAN_API}/api/asset/upload",
        data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read().decode('utf-8'))
    except Exception as e:
        log(f"  图片上传失败 {filename}: {e}", "WARN")
        return None
    if resp.get('code') != 0:
        log(f"  图片上传失败 {filename}: {resp.get('msg', '')}", "WARN")
        return None
    succ = resp.get('data', {}).get('succMap', {})
    return list(succ.values())[0] if succ else None


def siyuan_get_file(path):
    """读取思源工作区文件"""
    req = urllib.request.Request(
        f"{SIYUAN_API}/api/file/getFile",
        data=json.dumps({'path': path}).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    return urllib.request.urlopen(req, timeout=60).read()


def siyuan_put_file(path, data, mime):
    """写入思源工作区文件（multipart）"""
    boundary = "----wb" + str(int(time.time() * 1000)) + uuid.uuid4().hex[:6]
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="path"\r\n\r\n{path}\r\n'.encode(),
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="isDir"\r\n\r\nfalse\r\n'.encode(),
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="modTime"\r\n\r\n{int(time.time() * 1000)}\r\n'.encode(),
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="f"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        f"{SIYUAN_API}/api/file/putFile",
        data=b"".join(parts),
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode('utf-8'))


def fix_assets(notebook_id, doc_ids):
    """把误存到全局 /data/assets/ 的图片复制到笔记本 assets 目录，使相对链接生效"""
    if not doc_ids:
        return
    asset_names = set()
    for did in doc_ids:
        result = siyuan_request('/api/export/exportMdContent', {'id': did})
        if result.get('code') != 0:
            continue
        md = result.get('data', {}).get('content', '')
        asset_names.update(
            os.path.basename(p) for p in
            re.findall(r'!\[.*?\]\((assets/[^)]+)\)', md)
        )
    ok = 0
    for name in sorted(asset_names):
        nb_path = f"/data/{notebook_id}/assets/{name}"
        try:
            siyuan_get_file(nb_path)
            ok += 1  # 已存在
            continue
        except Exception:
            pass
        try:
            data = siyuan_get_file(f"/data/assets/{name}")
            mime = "image/png" if name.endswith(".png") else "image/jpeg"
            siyuan_put_file(nb_path, data, mime)
            ok += 1
        except Exception as e:
            log(f"  fix-assets 失败 {name}: {e}", "WARN")
    if asset_names:
        log(f"  fix-assets: {ok}/{len(asset_names)} 就位")


def build_body_from_blocks(idx, blocks, img_dir):
    """
    blocks → markdown 正文（文本段落 + 图片按原位置内联）。
    来自 siyuan_pipeline.py 的 build_body，含垃圾块过滤。
    """
    parts = []
    uploaded = {}
    for b in blocks:
        if b.get('type') == 'text':
            t = (b.get('text') or '').strip()
            if t and not is_junk_text(t):
                parts.append(t)
        elif b.get('type') == 'img' and b.get('file'):
            fp = os.path.join(img_dir, b['file'])
            if not os.path.exists(fp):
                continue
            if b['file'] not in uploaded:
                uploaded[b['file']] = upload_asset(fp, idx)
            p = uploaded[b['file']]
            if p:
                parts.append(f"![图片]({p})")
    return "\n\n".join(parts)


def export_md_content(doc_id):
    """导出文档 markdown 内容（verify 用）"""
    result = siyuan_request('/api/export/exportMdContent', {'id': doc_id})
    if result.get('code') != 0:
        return ''
    return result.get('data', {}).get('content', '')


# ============================================================
#  状态管理
# ============================================================

def load_state():
    """加载状态文件，不存在则返回空骨架"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log(f"状态文件损坏，备份后重建: {e}", "WARN")
            backup = STATE_FILE.with_suffix('.json.bak')
            shutil.copy2(STATE_FILE, backup)
            log(f"旧状态已备份到 {backup}")
    return {
        'created': datetime.now().isoformat(),
        'updated': datetime.now().isoformat(),
        'notebook_id': '',
        'scan_time': '',
        'total_docs': 0,
        'items': {},  # key = url_hash
    }


def save_state(state):
    """原子保存状态"""
    state['updated'] = datetime.now().isoformat()
    atomic_write_json(STATE_FILE, state)


def get_item(state, url, create=True, doc_id=None):
    """获取或创建一个 URL 对应的状态项"""
    h = url_hash(url)
    if h not in state['items'] and create:
        state['items'][h] = {
            'url': url,
            'normalized_url': normalize_zhihu_url(url),
            'status': 'pending',       # pending|deleted|fetched|created|verified|done|failed_X
            'old_doc_id': doc_id or '',
            'old_hpath': '',
            'new_doc_id': '',
            'title': '',
            'error': '',
            'attempts': 0,
            'last_attempt': '',
            'updated': '',
        }
    return state['items'].get(h)


# ============================================================
#  阶段 1：扫描 (scan)
# ============================================================

def get_all_doc_ids(notebook_id=None):
    """
    获取所有文档块（type=d 的文档头块）。

    思源 SQL 引擎有严重的索引问题：type='d' 的批量查询会随机漏掉部分文档
    （尤其是标题为长 URL、含特殊字符、或刚创建的文档）。
    因此采用三重策略取并集 + 文件树遍历兜底，确保零遗漏：

      策略1: SQL WHERE type = 'd'
      策略2: SQL DISTINCT root_id
      策略3: SQL hpath != ''
      策略4: 文件树 API 递归遍历（完全绕过 SQL，最可靠）

    所有策略的结果取并集，对缺失字段的文档逐个查询补全。
    """
    doc_map = {}  # id -> {id, content, fcontent, hpath}

    # --- 策略1: 标准 type='d' 查询 ---
    r1 = siyuan_request('/api/query/sql', {
        'stmt': "SELECT id, content, fcontent, hpath FROM blocks WHERE type = 'd'"
    })
    for d in r1.get('data', []):
        doc_map[d['id']] = d

    # --- 策略2: DISTINCT root_id 补充 ---
    r2 = siyuan_request('/api/query/sql', {
        'stmt': "SELECT DISTINCT root_id AS id FROM blocks WHERE root_id != ''"
    })
    for d in r2.get('data', []):
        rid = d.get('id', '')
        if rid and rid not in doc_map:
            doc_map[rid] = {'id': rid, 'content': '', 'fcontent': '', 'hpath': ''}

    # --- 策略3: hpath != '' 补充 ---
    r3 = siyuan_request('/api/query/sql', {
        'stmt': "SELECT DISTINCT id, content, fcontent, hpath FROM blocks WHERE hpath != '' AND type = 'd'"
    })
    for d in r3.get('data', []):
        if d['id'] not in doc_map:
            doc_map[d['id']] = d

    # --- 策略4: 文件树 API 递归遍历（兜底，最可靠）---
    if not notebook_id:
        notebook_id = check_api()
    if notebook_id:
        tree_docs = _walk_file_tree(notebook_id)
        for td in tree_docs:
            tid = td['id']
            if tid not in doc_map:
                doc_map[tid] = {
                    'id': tid,
                    'content': td.get('name', ''),
                    'fcontent': td.get('name', ''),
                    'hpath': td.get('path', ''),
                }
            else:
                # 文件树有 name 信息，补全空 content
                if not doc_map[tid].get('content'):
                    doc_map[tid]['content'] = td.get('name', '')
                    doc_map[tid]['fcontent'] = td.get('name', '')

    # --- 对仍缺失字段的文档，逐个查询补全 ---
    need_fetch = [did for did, d in doc_map.items()
                  if not d.get('hpath')]
    for did in need_fetch:
        r = siyuan_request('/api/query/sql', {
            'stmt': f"SELECT id, content, fcontent, hpath FROM blocks WHERE id = '{did}'"
        })
        if r.get('data'):
            existing = doc_map[did]
            fetched = r['data'][0]
            # 合并：不覆盖已有非空值
            for k in ('content', 'fcontent', 'hpath'):
                if not existing.get(k) and fetched.get(k):
                    existing[k] = fetched[k]

    return list(doc_map.values())


def _walk_file_tree(notebook_id, path='/', depth=0):
    """
    递归遍历文件树，返回所有文档。
    listDocsByPath 返回 {box, files: [...]}，files 中每项有 id/name/path/subFileCount。
    subFileCount > 0 的为目录（含子文档），需递归。
    """
    if depth > 15:  # 防止无限递归
        return []
    result = siyuan_request('/api/filetree/listDocsByPath', {
        'notebook': notebook_id,
        'path': path
    })
    data = result.get('data')
    if not isinstance(data, dict):
        return []
    files = data.get('files', [])
    all_docs = []
    for f in files:
        fid = f.get('id', '')
        fname = f.get('name', '')
        fpath = f.get('path', '')
        sub_count = f.get('subFileCount', 0)
        if fid:
            all_docs.append({'id': fid, 'name': fname, 'path': fpath})
        # 递归子目录（subFileCount>0 或 path 以 .sy 结尾且含子路径时）
        if sub_count > 0:
            child_path = fpath
            if child_path.endswith('.sy'):
                # 去掉 .sy 后缀得到目录路径
                child_path = child_path[:-3]
            if child_path and child_path != path:
                all_docs.extend(_walk_file_tree(notebook_id, child_path, depth + 1))
    return all_docs


def get_doc_body_text(doc_id):
    """获取文档正文文本（合并所有子块的 content）"""
    # 注意：SQL 中的 doc_id 是纯字母数字+连字符，无需额外转义
    sql = (
        f"SELECT content FROM blocks "
        f"WHERE root_id = '{doc_id}' AND type != 'd' "
        f"ORDER BY sort"
    )
    result = siyuan_request('/api/query/sql', {'stmt': sql})
    if result.get('code') != 0:
        return ''
    blocks = result.get('data', [])
    return ' '.join((b.get('content') or '') for b in blocks)


def is_failed_note(title, hpath, body_text):
    """
    判断是否为剪藏失败笔记。
    返回 (is_failed, reason, urls)
    """
    title_norm = normalize_text(title)

    # 策略1：标题本身是知乎链接
    title_urls = extract_zhihu_urls(title_norm)
    if title_urls:
        return True, "标题为知乎链接", title_urls

    # 策略2：标题为"无标题笔记"或"未命名文档"
    title_stripped = title.strip()
    if title_stripped in ('无标题笔记', '未命名文档'):
        body_urls = extract_zhihu_urls(body_text)
        if body_urls:
            return True, f"标题为「{title_stripped}」", body_urls

    # 策略3：正文含"剪藏失败"标志且含知乎链接
    failure_markers = ['剪藏失败', '可在桌面端', '使用剪藏插件', '原文链接']
    if any(m in body_text for m in failure_markers):
        body_urls = extract_zhihu_urls(body_text)
        if body_urls:
            return True, "正文含剪藏失败标志", body_urls

    # 策略4：正文极短（≤3段）且仅含知乎链接无实质内容
    if body_text and len(body_text) < 300:
        body_urls = extract_zhihu_urls(body_text)
        # 去掉链接后剩余文字极少
        remaining = ZHIHU_URL_PATTERN.sub('', normalize_text(body_text)).strip()
        if body_urls and len(remaining) < 50:
            return True, "正文仅含知乎链接无内容", body_urls

    return False, "", []


def cmd_scan(args):
    """扫描所有笔记，识别失败笔记"""
    log("=" * 60)
    log("阶段 1/5：扫描思源笔记，识别剪藏失败笔记")
    log("=" * 60)

    notebook_id = check_api()
    if not notebook_id:
        sys.exit(1)

    # 如果是 --rescan，保留旧状态但重新扫描
    state = load_state() if not args.rescan else {
        'created': datetime.now().isoformat(),
        'notebook_id': notebook_id,
        'items': {},
    }
    state['notebook_id'] = notebook_id

    log("获取所有文档...")
    docs = get_all_doc_ids(notebook_id)
    state['total_docs'] = len(docs)
    log(f"共 {len(docs)} 篇文档，开始检测...")

    found = 0
    batch_count = 0
    for i, doc in enumerate(docs):
        doc_id = doc['id']
        title = doc.get('content', '') or doc.get('fcontent', '') or ''
        hpath = doc.get('hpath', '')

        # 对疑似笔记才查正文（减少 API 调用）
        title_norm = normalize_text(title)
        title_has_url = bool(extract_zhihu_urls(title_norm))
        title_suspect = title.strip() in ('无标题笔记', '未命名文档') or title_has_url

        body_text = ''
        if title_suspect:
            body_text = get_doc_body_text(doc_id)

        is_failed, reason, urls = is_failed_note(title, hpath, body_text)

        if is_failed:
            for url in urls:
                norm_url = normalize_zhihu_url(url)
                item = get_item(state, norm_url, create=True, doc_id=doc_id)
                item['old_hpath'] = hpath
                if item['status'] == 'pending':
                    item['status'] = 'pending'
            found += 1
            if found <= 20 or found % 50 == 0:
                log(f"  [{found}] {hpath} — {reason}")

        # 每50条保存一次状态（防止中途崩溃丢失）
        batch_count += 1
        if batch_count >= 50:
            save_state(state)
            batch_count = 0

    state['scan_time'] = datetime.now().isoformat()
    save_state(state)

    total_urls = len(state['items'])
    log("")
    log(f"扫描完成：{found} 篇失败笔记，{total_urls} 个待处理知乎链接")
    log(f"状态已保存: {STATE_FILE}")

    if total_urls > 0:
        log("")
        log("下一步：")
        log("  python zhihu_reclip.py delete    # 删除旧失败笔记")
        log("  python zhihu_reclip.py prepare   # 导出待抓取清单")


# ============================================================
#  阶段 2：删除旧笔记 (delete)
# ============================================================

def cmd_delete(args):
    """删除所有已识别的旧失败笔记"""
    log("=" * 60)
    log("阶段 2/5：删除旧的剪藏失败笔记")
    log("=" * 60)

    state = load_state()
    if not state.get('items'):
        log("状态为空，请先运行 scan", "ERROR")
        sys.exit(1)

    notebook_id = ensure_notebook_id(state)
    if not notebook_id:
        sys.exit(1)

    pending = [(h, item) for h, item in state['items'].items()
               if item['status'] == 'pending' and item.get('old_doc_id')]
    log(f"待删除: {len(pending)} 篇")

    if not pending:
        log("没有待删除的笔记（可能已删除）")
        return

    deleted, failed, skipped = 0, 0, 0
    for i, (h, item) in enumerate(pending):
        doc_id = item['old_doc_id']
        result = siyuan_request('/api/filetree/removeDoc', {
            'notebook': notebook_id,
            'path': f'/{doc_id}.sy'
        })
        if result.get('code') == 0:
            item['status'] = 'deleted'
            deleted += 1
        else:
            # 已经不存在也算成功（幂等）
            msg = result.get('msg', '')
            if 'not found' in msg.lower() or '不存在' in msg:
                item['status'] = 'deleted'
                skipped += 1
            else:
                item['status'] = 'failed_delete'
                item['error'] = msg
                item['attempts'] += 1
                failed += 1
                log(f"  删除失败 {doc_id}: {msg}", "WARN")

        item['last_attempt'] = datetime.now().isoformat()

        # 每20条保存一次
        if (i + 1) % 20 == 0:
            save_state(state)
            log(f"  进度: {i + 1}/{len(pending)} (删除 {deleted}, 失败 {failed})")

    save_state(state)
    log(f"删除完成: 成功 {deleted}, 跳过(已不存在) {skipped}, 失败 {failed}")
    if failed:
        log("失败的笔记可用 reset 命令重试", "WARN")


# ============================================================
#  阶段 3：导出待抓取清单 (prepare)
# ============================================================

def cmd_prepare(args):
    """导出待抓取的 URL 清单，供 WorkBuddy 逐条抓取"""
    log("=" * 60)
    log("阶段 3/5：导出待抓取 URL 清单")
    log("=" * 60)

    state = load_state()
    if not state.get('items'):
        log("状态为空，请先运行 scan", "ERROR")
        sys.exit(1)

    # 收集所有需要抓取的 URL
    to_fetch = []
    for h, item in state['items'].items():
        if item['status'] in ('deleted', 'pending', 'failed_fetch'):
            to_fetch.append({
                'hash': h,
                'url': item['normalized_url'],
            })

    if not to_fetch:
        log("没有待抓取的 URL")
        return

    # 写入 JSONL 格式（每行一个 JSON，方便逐行读取）
    with open(FETCH_QUEUE_FILE, 'w', encoding='utf-8') as f:
        for entry in to_fetch:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    log(f"已导出 {len(to_fetch)} 个 URL 到 {FETCH_QUEUE_FILE}")
    log("")
    log("WorkBuddy 抓取流程：")
    log("  1. 对每个 URL 用 WebFetch 抓取知乎内容")
    log(f"  2. 结果保存到 {FETCH_INPUT_DIR}/<hash>.json")
    log("     格式: {\"url\":\"...\", \"title\":\"...\", \"markdown\":\"...\"}")
    log("  3. 运行 process 阶段创建思源笔记")
    log("")
    log(f"也可查看 {FETCH_QUEUE_FILE} 了解待抓取清单")


# ============================================================
#  阶段 3b：Playwright 批量抓取 (fetch)
#  来自 zhihu-to-siyuan skill 的核心经验：
#  WebFetch 抓知乎图片全丢/长文截断/抓错回答 → 必须走 Playwright 本机 Chrome
# ============================================================

def generate_articles_json(state, limit=None):
    """
    从状态生成 articles.json（zhihu_extract.js 的输入格式）。
    已有抓取产出的 URL 自动跳过（断点续传）。
    返回 (articles, skipped)。
    """
    items = []
    for h, item in state['items'].items():
        if item['status'] not in ('pending', 'deleted', 'failed_fetch'):
            continue
        # 已有抓取产出的跳过
        idx = item.get('idx', '')
        if idx and (EXTRACT_DIR / f"{idx}_blocks.json").exists():
            continue
        items.append((h, item))

    items.sort(key=lambda x: x[1]['normalized_url'])
    if limit:
        items = items[:limit]

    articles = []
    # 沿用已有编号，保证重跑不冲突
    used_idx = {it.get('idx') for it in state['items'].values() if it.get('idx')}
    next_num = 1
    for h, item in items:
        while f"{next_num:04d}" in used_idx:
            next_num += 1
        idx = f"{next_num:04d}"
        used_idx.add(idx)
        url_type = detect_url_type(item['normalized_url'])
        item['idx'] = idx
        item['fetch_type'] = url_type
        articles.append({
            'idx': idx,
            'type': url_type,
            'url': item['normalized_url'],
            'title': '',            # 抓取后从 summary 回填
            'hash': h,
            'docId': '',
        })
    return articles, len(items) and len(state['items']) - len(items) or 0


def cmd_fetch(args):
    """生成 articles.json 并调用 zhihu_extract.js 批量抓取"""
    log("=" * 60)
    log("阶段 3b：Playwright 批量抓取知乎内容（完整保真链路）")
    log("=" * 60)

    if not os.path.exists(ZHIHU_EXTRACT_JS):
        log(f"zhihu_extract.js 不存在: {ZHIHU_EXTRACT_JS}", "ERROR")
        log("请设置环境变量 ZHIHU_EXTRACT_JS 指向正确路径", "ERROR")
        sys.exit(1)
    if not os.path.exists(NODE_EXE):
        log(f"node 不存在: {NODE_EXE}", "ERROR")
        sys.exit(1)

    state = load_state()
    if not state.get('items'):
        log("状态为空，请先运行 scan", "ERROR")
        sys.exit(1)

    articles, _ = generate_articles_json(state, limit=args.limit)
    if not articles:
        log("没有需要抓取的 URL（可能已全部抓取完成）")
        save_state(state)
        return

    # 统计类型分布
    type_counts = {}
    for a in articles:
        type_counts[a['type']] = type_counts.get(a['type'], 0) + 1
    log(f"待抓取 {len(articles)} 个 URL: {type_counts}")
    unknown = [a['url'] for a in articles if a['type'] in ('unknown', 'question')]
    if unknown:
        log(f"警告: {len(unknown)} 个 URL 无法识别类型（问题页/未知），抓取可能失败", "WARN")
        for u in unknown[:5]:
            log(f"  {u}", "WARN")

    with open(ARTICLES_FILE, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
    save_state(state)  # 保存 idx 分配
    log(f"已生成 {ARTICLES_FILE}")

    # 调用 zhihu_extract.js
    env = os.environ.copy()
    env['NODE_PATH'] = os.path.join(NODE_WORKSPACE, 'node_modules')
    env['PLAYWRIGHT_BROWSERS_PATH'] = PW_BROWSERS_PATH

    import subprocess
    log(f"启动抓取: {NODE_EXE} {ZHIHU_EXTRACT_JS} articles.json {EXTRACT_DIR}")
    log(f"  （千条规模建议分批: python zhihu_reclip.py fetch --limit 50）")
    proc = subprocess.run(
        [NODE_EXE, ZHIHU_EXTRACT_JS, str(ARTICLES_FILE.name), str(EXTRACT_DIR.name)],
        cwd=str(WORK_DIR),
        env=env,
    )

    # 检查抓取结果，回填状态
    EXTRACT_DIR.mkdir(exist_ok=True)
    fetched, missing = 0, 0
    for a in articles:
        h = a['hash']
        item = state['items'].get(h)
        if not item:
            continue
        bf = EXTRACT_DIR / f"{a['idx']}_blocks.json"
        if bf.exists():
            item['status'] = 'fetched'
            fetched += 1
        else:
            item['status'] = 'failed_fetch'
            item['error'] = '抓取无产出（可能被反爬/需要登录/类型不支持）'
            missing += 1
    save_state(state)

    log("")
    log(f"抓取完成: 成功 {fetched}, 无产出 {missing} (exit={proc.returncode})")
    summary_file = EXTRACT_DIR / 'summary.json'
    if summary_file.exists():
        log(f"汇总见 {summary_file}")
    if missing:
        log("无产出的 URL 可用 reset --status failed_fetch 重试", "WARN")
    log("下一步: python zhihu_reclip.py process")


# ============================================================
#  阶段 4：处理抓取内容，创建笔记 (process)
# ============================================================

def _load_extract_data(item):
    """
    读取 Playwright 抓取产物，返回 (title, markdown) 或 None。
    优先链路 B（blocks + 图片），pin 无标题时取正文开头（skill 经验）。
    """
    idx = item.get('idx', '')
    if not idx:
        return None
    bf = EXTRACT_DIR / f"{idx}_blocks.json"
    if not bf.exists():
        return None
    with open(bf, encoding='utf-8') as f:
        data = json.load(f)
    blocks = data.get('blocks', [])

    title = item.get('title', '')
    if not title:
        # 从 summary.json 取标题
        sf = EXTRACT_DIR / 'summary.json'
        if sf.exists():
            try:
                with open(sf, encoding='utf-8') as f:
                    summaries = json.load(f)
                for s in (summaries if isinstance(summaries, list) else summaries.get('items', [])):
                    if s.get('idx') == idx and s.get('title'):
                        title = s['title']
                        break
            except Exception:
                pass
    if not title:
        # pin/无标题场景：取第一个文本块开头（zhihu-to-siyuan 经验）
        for b in blocks:
            if b.get('type') == 'text' and (b.get('text') or '').strip():
                title = b['text'].strip()[:40]
                break

    body = build_body_from_blocks(idx, blocks, str(EXTRACT_DIR / 'imgs'))
    return title, body


def _load_webfetch_data(h, item):
    """降级链路 A：读取 WorkBuddy WebFetch 抓取的结果文件"""
    cf = FETCH_INPUT_DIR / f"{h}.json"
    if not cf.exists():
        return None
    try:
        with open(cf, 'r', encoding='utf-8') as f:
            content = json.load(f)
        title = (content.get('title') or '').strip()
        markdown = (content.get('markdown') or '').strip()
        if title and markdown:
            return title, markdown
    except (json.JSONDecodeError, IOError):
        pass
    return None


def cmd_process(args):
    """读取抓取好的内容，批量创建思源笔记（支持 Playwright/WebFetch 双链路）"""
    log("=" * 60)
    log("阶段 4/5：创建新笔记")
    log("=" * 60)

    state = load_state()
    if not state.get('items'):
        log("状态为空，请先运行 scan", "ERROR")
        sys.exit(1)

    notebook_id = ensure_notebook_id(state)
    if not notebook_id:
        sys.exit(1)

    FETCH_INPUT_DIR.mkdir(exist_ok=True)
    EXTRACT_DIR.mkdir(exist_ok=True)
    target_path = args.path or '/'

    to_create = []
    for h, item in state['items'].items():
        if item['status'] in ('deleted', 'pending', 'failed_create', 'fetched'):
            to_create.append((h, item))

    log(f"待创建: {len(to_create)} 篇")
    if not to_create:
        log("没有待创建的笔记")
        return

    created, failed, skipped = 0, 0, 0
    created_doc_ids = []
    for i, (h, item) in enumerate(to_create):
        url = item['normalized_url']

        # 链路 B: Playwright blocks（含图片，保真）
        extract = _load_extract_data(item)
        # 链路 A: WebFetch 纯文本（降级兜底）
        webfetch = None if extract else _load_webfetch_data(h, item)

        if extract:
            title, body = extract
            markdown = f"> 来源：[知乎]({url})\n\n{body}"
        elif webfetch:
            title, markdown = webfetch
            markdown = f"> 来源：[知乎]({url})\n\n{markdown}"
        else:
            skipped += 1
            if skipped <= 5:
                log(f"  跳过 {h}: 无抓取内容 ({url})", "WARN")
            continue

        if not title or not markdown.strip():
            item['status'] = 'failed_create'
            item['error'] = '标题或正文为空'
            item['attempts'] += 1
            failed += 1
            continue

        # 创建文档
        result = siyuan_request('/api/filetree/createDocWithMd', {
            'notebook': notebook_id,
            'path': target_path,
            'markdown': markdown,
            'title': title
        })

        if result.get('code') != 0:
            item['status'] = 'failed_create'
            item['error'] = result.get('msg', '创建失败')
            item['attempts'] += 1
            failed += 1
            log(f"  创建失败 {h}: {item['error']}", "WARN")
            continue

        new_doc_id = result['data']

        # 修复标题（关键两步，缺一不可——skill 核心经验）
        r1 = siyuan_request('/api/filetree/renameDoc', {
            'notebook': notebook_id,
            'path': f'/{new_doc_id}.sy',
            'title': title
        })
        r2 = siyuan_request('/api/attr/setBlockAttrs', {
            'id': new_doc_id,
            'attrs': {
                'custom-sy-title-empty': 'false',
                'title': title
            }
        })

        if r1.get('code') == 0 and r2.get('code') == 0:
            item['status'] = 'created'
            item['new_doc_id'] = new_doc_id
            item['title'] = title
            created += 1
            created_doc_ids.append(new_doc_id)
        else:
            # 文档已创建但标题修复失败——标记为待手动修复
            item['status'] = 'created_title_issue'
            item['new_doc_id'] = new_doc_id
            item['title'] = title
            item['error'] = f"renameDoc={r1.get('code')}, setBlockAttrs={r2.get('code')}"
            created_doc_ids.append(new_doc_id)
            log(f"  标题修复异常 {h} (doc={new_doc_id}): {item['error']}", "WARN")

        item['last_attempt'] = datetime.now().isoformat()

        # 保存进度（千条规模：每 10 条落盘 + 每 100 条输出进度，migrate_v3 经验）
        if (i + 1) % 10 == 0:
            save_state(state)
        if (i + 1) % 100 == 0:
            elapsed_note = f"{i + 1}/{len(to_create)}"
            log(f"  进度: {elapsed_note} (创建 {created}, 失败 {failed}, 跳过 {skipped})")

    save_state(state)

    # 图片资产归位（链路 B 的图片先落在全局 /data/assets/，需复制到笔记本目录）
    if created_doc_ids:
        log("归位图片资产 (fix-assets)...")
        fix_assets(notebook_id, created_doc_ids)

    log(f"创建完成: 成功 {created}, 失败 {failed}, 跳过 {skipped}")
    log("下一步: python zhihu_reclip.py verify")


# ============================================================
#  阶段 5：验证 (verify)
# ============================================================

def cmd_verify(args):
    """验证新文档：标题正确 + 内容命中率（来自 siyuan_pipeline verify 经验）"""
    log("=" * 60)
    log("阶段 5/5：验证文档")
    log("=" * 60)

    state = load_state()
    verified, issues = 0, 0

    norm = lambda s: "".join((s or '').split())

    for h, item in state['items'].items():
        if item['status'] not in ('created', 'created_title_issue', 'verified', 'done'):
            continue

        new_doc_id = item.get('new_doc_id', '')
        if not new_doc_id:
            continue

        # 1) 标题校验
        result = siyuan_request('/api/attr/getBlockAttrs', {'id': new_doc_id})
        attrs = result.get('data', {})
        actual_title = attrs.get('title', '')
        empty_flag = attrs.get('custom-sy-title-empty', 'true')
        title_ok = (actual_title == item['title'] and empty_flag == 'false')

        # 2) 内容命中率校验（Playwright 链路才有 blocks）
        content_ok = True
        hit_rate = -1
        idx = item.get('idx', '')
        bf = EXTRACT_DIR / f"{idx}_blocks.json" if idx else None
        if title_ok and bf and bf.exists():
            md_norm = norm(export_md_content(new_doc_id))
            if md_norm:
                try:
                    with open(bf, encoding='utf-8') as f:
                        blocks = json.load(f).get('blocks', [])
                    texts = [b['text'] for b in blocks
                             if b.get('type') == 'text' and (b.get('text') or '').strip()
                             and not is_junk_text(b['text'])]
                    if texts:
                        hit = sum(1 for t in texts if norm(t)[:15] in md_norm)
                        hit_rate = hit * 100 // len(texts)
                        # 纯图片回答 blocks 里可能只有 img，texts 为空属正常
                        content_ok = hit_rate >= 80
                except Exception:
                    pass

        if title_ok and content_ok:
            item['status'] = 'verified'
            item['hit_rate'] = hit_rate
            verified += 1
        else:
            issues += 1
            reason = []
            if not title_ok:
                reason.append(f"标题: 期望='{item['title']}' 实际='{actual_title}' empty={empty_flag}")
            if not content_ok:
                reason.append(f"内容命中率 {hit_rate}% < 80%")
            if issues <= 10:
                log(f"  异常 {h}: {'; '.join(reason)}", "WARN")

    save_state(state)
    log(f"验证完成: 正常 {verified}, 异常 {issues}")


# ============================================================
#  辅助命令
# ============================================================

def cmd_status(args):
    """查看当前进度（只读）"""
    state = load_state()
    items = state.get('items', {})

    # 按状态统计
    status_counts = {}
    for item in items.values():
        s = item['status']
        status_counts[s] = status_counts.get(s, 0) + 1

    log("=" * 60)
    log("当前状态")
    log("=" * 60)
    log(f"  总笔记数: {state.get('total_docs', '?')}")
    log(f"  待处理链接: {len(items)}")
    log(f"  扫描时间: {state.get('scan_time', '未扫描')}")
    log(f"  最后更新: {state.get('updated', '')}")
    log("")
    log("状态分布:")
    for s, c in sorted(status_counts.items()):
        log(f"  {s:25s} {c:6d}")

    # 未完成的项
    incomplete = sum(c for s, c in status_counts.items()
                     if s not in ('done', 'verified'))
    log(f"\n未完成: {incomplete}")

    if incomplete > 0:
        log("\n下一步建议:")
        if status_counts.get('pending', 0) > 0:
            log("  python zhihu_reclip.py delete   # 删除旧笔记")
        if status_counts.get('deleted', 0) > 0:
            log("  python zhihu_reclip.py prepare  # 导出抓取清单")
        if any(status_counts.get(s, 0) > 0 for s in ('deleted', 'fetched', 'failed_create')):
            log("  python zhihu_reclip.py process  # 创建新笔记")
        if status_counts.get('created', 0) > 0 or status_counts.get('created_title_issue', 0) > 0:
            log("  python zhihu_reclip.py verify   # 验证标题")


def cmd_reset(args):
    """重置指定状态项，允许重试"""
    state = load_state()
    target_status = args.status or 'failed'
    reset_count = 0

    for h, item in state['items'].items():
        if target_status == 'all' or item['status'].startswith(target_status):
            # 不要重置已成功创建的笔记！
            if item['status'] in ('created', 'verified', 'done'):
                continue
            # 退回到上一个安全状态
            if item['status'].startswith('failed_delete'):
                item['status'] = 'pending'
            elif item['status'].startswith('failed_fetch'):
                item['status'] = 'deleted'
            elif item['status'].startswith('failed_create'):
                item['status'] = 'deleted'
                # 清除可能存在的不完整新文档引用
                item['new_doc_id'] = ''
            else:
                continue
            item['error'] = ''
            reset_count += 1

    save_state(state)
    log(f"已重置 {reset_count} 项")


def categorize_error(err):
    """失败分类（来自 migrate_v3.py 的失败分类经验）"""
    e = (err or '').lower()
    if 'timeout' in e or 'timed out' in e:
        return 'timeout'
    if 'connection' in e or 'winerror' in e or 'urlopen' in e:
        return 'connection'
    if 'http 5' in e:
        return 'server_error'
    if 'http 4' in e:
        return 'client_error'
    if '反爬' in (err or '') or '抓取无产出' in (err or '') or '登录' in (err or ''):
        return 'anti_scraping'
    if 'path' in e:
        return 'path_too_long'
    if 'block not found' in e:
        return 'block_not_found'
    return 'other'


def cmd_report(args):
    """生成最终报告（含 deep link 和失败分类——migrate_v3 经验）"""
    state = load_state()
    items = state.get('items', {})

    report = {
        'report_time': datetime.now().isoformat(),
        'total_docs': state.get('total_docs', 0),
        'total_urls': len(items),
        'summary': {},
        'failure_categories': {},
        'failed_items': [],
        'success_items': [],
    }

    for h, item in items.items():
        s = item['status']
        report['summary'][s] = report['summary'].get(s, 0) + 1
        if s in ('verified', 'done'):
            doc_id = item.get('new_doc_id', '')
            report['success_items'].append({
                'url': item['normalized_url'],
                'title': item.get('title', ''),
                'doc_id': doc_id,
                'deep_link': f"{SIYUAN_API}/stage/build/desktop/?id={doc_id}" if doc_id else '',
                'hit_rate': item.get('hit_rate', -1),
            })
        elif s.startswith('failed') or s == 'created_title_issue':
            err = item.get('error', '')
            cat = categorize_error(err)
            report['failure_categories'][cat] = report['failure_categories'].get(cat, 0) + 1
            report['failed_items'].append({
                'url': item['normalized_url'],
                'status': s,
                'category': cat,
                'error': err,
            })

    report_path = WORK_DIR / 'reclip_final_report.json'
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    md_path = WORK_DIR / 'reclip_final_report.md'
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(f"# 知乎剪藏重建报告\n\n")
        f.write(f"生成时间: {report['report_time']}\n\n")
        f.write("## 状态分布\n\n| 状态 | 数量 |\n|---|---|\n")
        for s, c in sorted(report['summary'].items()):
            f.write(f"| {s} | {c} |\n")
        if report['failure_categories']:
            f.write("\n## 失败分类\n\n| 类别 | 数量 |\n|---|---|\n")
            for c, n in sorted(report['failure_categories'].items()):
                f.write(f"| {c} | {n} |\n")
        f.write(f"\n## 成功清单 ({len(report['success_items'])})\n\n")
        f.write("| 标题 | deep link | 命中率 |\n|---|---|---|\n")
        for it in report['success_items']:
            rate = f"{it['hit_rate']}%" if it['hit_rate'] >= 0 else '-'
            f.write(f"| {it['title'][:30]} | [打开]({it['deep_link']}) | {rate} |\n")
        if report['failed_items']:
            f.write(f"\n## 失败清单 ({len(report['failed_items'])})\n\n")
            f.write("| URL | 状态 | 类别 | 错误 |\n|---|---|---|---|\n")
            for it in report['failed_items'][:200]:
                f.write(f"| {it['url'][:50]} | {it['status']} | {it['category']} | {it['error'][:40]} |\n")

    log("=" * 60)
    log("最终报告")
    log("=" * 60)
    for s, c in sorted(report['summary'].items()):
        log(f"  {s:25s} {c:6d}")
    if report['failure_categories']:
        log("失败分类:")
        for c, n in sorted(report['failure_categories'].items()):
            log(f"  {c:25s} {n:6d}")
    log(f"\n成功: {len(report['success_items'])}")
    log(f"失败: {len(report['failed_items'])}")
    log(f"报告: {report_path}")
    log(f"     {md_path}")


# ============================================================
#  主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='知乎剪藏失败笔记修复工具（支持千条规模，断点续传）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
完整流程（印象笔记迁移到思源后执行）：

  # 1. 扫描发现失败笔记
  python zhihu_reclip.py scan

  # 2. 删除旧的失败笔记
  python zhihu_reclip.py delete

  # 3. Playwright 批量抓取知乎内容（完整保真链路，推荐）
  python zhihu_reclip.py fetch --limit 50

  # 3b. （可选降级）导出清单给 WorkBuddy WebFetch 抓取，结果放 fetched_content/
  python zhihu_reclip.py prepare

  # 4. 读取抓取内容，创建思源笔记
  python zhihu_reclip.py process

  # 5. 验证标题 + 内容命中率
  python zhihu_reclip.py verify

  # 随时可用：
  python zhihu_reclip.py status           # 查看进度
  python zhihu_reclip.py reset --status failed  # 重试失败项
  python zhihu_reclip.py report           # 生成最终报告

所有阶段都支持断点续传——任何时刻中断，重新运行同一命令即可恢复。
        """
    )

    sub = parser.add_subparsers(dest='command')

    p_scan = sub.add_parser('scan', help='扫描思源，识别失败笔记')
    p_scan.add_argument('--rescan', action='store_true', help='丢弃旧状态，完全重新扫描')
    sub.add_parser('delete', help='删除旧的失败笔记')
    sub.add_parser('prepare', help='导出待抓取 URL 清单（WebFetch 降级链路）')
    p_fetch = sub.add_parser('fetch', help='Playwright 批量抓取知乎内容（推荐，完整保真）')
    p_fetch.add_argument('--limit', type=int, default=None,
                         help='本次最多抓取 N 个 URL（千条规模分批用）')
    p_process = sub.add_parser('process', help='读取抓取内容，创建新笔记')
    p_process.add_argument('--path', default='/', help='创建路径（默认 /）')
    sub.add_parser('verify', help='验证文档标题')
    sub.add_parser('status', help='查看当前进度')
    p_reset = sub.add_parser('reset', help='重置失败项以便重试')
    p_reset.add_argument('--status', default='failed', help='重置哪些状态（默认 failed）')
    sub.add_parser('report', help='生成最终报告')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # 确保 cache 目录存在
    CONTENT_CACHE_DIR.mkdir(exist_ok=True)
    FETCH_INPUT_DIR.mkdir(exist_ok=True)

    commands = {
        'scan': cmd_scan,
        'delete': cmd_delete,
        'prepare': cmd_prepare,
        'fetch': cmd_fetch,
        'process': cmd_process,
        'verify': cmd_verify,
        'status': cmd_status,
        'reset': cmd_reset,
        'report': cmd_report,
    }
    commands[args.command](args)


if __name__ == '__main__':
    main()
