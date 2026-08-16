# -*- coding: utf-8 -*-
"""知乎 -> 思源笔记 写入流水线（配合 zhihu_extract.js 使用）

子命令:
  create      从抓取的 blocks 创建新文档（含图片内联 + 标题修复 + 资产归位）
  rebuild     重建已有文档（文档存在但内容不完整/错误时用）
  fix-assets  把误存到全局 /data/assets 的图片复制到笔记本 assets 目录
  verify      核对抓取内容与文档的匹配度

用法示例:
  python siyuan_pipeline.py create --notebook <笔记本ID> --articles articles.json --extract-dir zhihu_extract
  python siyuan_pipeline.py rebuild --notebook <笔记本ID> --articles articles.json --extract-dir zhihu_extract
  python siyuan_pipeline.py fix-assets --notebook <笔记本ID> --articles articles.json
  python siyuan_pipeline.py verify --articles articles.json --extract-dir zhihu_extract

articles.json 中的 docId 字段会在 create 后自动回写，供后续子命令使用。

思源 API 关键坑（均已实测验证）:
  1. updateBlock 对文档根块(type=d)返回 code=0 但不持久化 —— 必须"删子块 + insertBlock"
  2. /api/asset/upload 上传的文件落在全局 /data/assets/，而文档内 assets/ 相对链接
     按笔记本目录解析 —— 需要 putFile 复制到 /data/<notebook>/assets/
  3. asset/upload 的文件字段名是 "file[]"（不是 files[]）
  4. SQL 查询端点 /api/query/sql，参数名 stmt
  5. createDocWithMd 标题不生效 —— 需要 renameDoc + setBlockAttrs(custom-sy-title-empty=false)
"""
import argparse
import json
import mimetypes
import os
import re
import time
import urllib.request
import uuid

API = "http://127.0.0.1:6806"

JUNK_PATTERNS = [
    re.compile(r"\d+\s*赞同"),
    re.compile(r"^发布于"),
    re.compile(r"^编辑于"),
    re.compile(r"^\d+\s*人赞同了该回答"),
]


# ---------- 基础 API ----------

def post_json(endpoint, payload):
    req = urllib.request.Request(
        API + endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req))


def export_md(doc_id):
    return post_json("/api/export/exportMdContent", {"id": doc_id})["data"]["content"]


def get_file(path):
    req = urllib.request.Request(
        API + "/api/file/getFile",
        data=json.dumps({"path": path}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req).read()


def put_file(path, data, mime):
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
        API + "/api/file/putFile",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return json.load(urllib.request.urlopen(req))


def upload_asset(filepath, rename_prefix):
    """上传图片资产。注意: 落在全局 /data/assets/，之后需 fix-assets 归位。"""
    filename = f"zhihu_{rename_prefix}_{os.path.basename(filepath)}"
    mime = mimetypes.guess_type(filepath)[0] or "image/jpeg"
    boundary = uuid.uuid4().hex
    with open(filepath, "rb") as f:
        filebytes = f.read()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file[]"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        filebytes,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        API + "/api/asset/upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    resp = json.load(urllib.request.urlopen(req))
    if resp.get("code") != 0:
        raise RuntimeError(f"asset upload failed: {resp}")
    succ = resp["data"]["succMap"]
    return list(succ.values())[0] if succ else None


# ---------- 内容构建 ----------

def is_junk(text):
    return any(p.search(text) for p in JUNK_PATTERNS)


def build_body(idx, blocks, img_dir):
    """blocks -> markdown 正文（文本段落 + 图片按原位置内联）"""
    parts = []
    uploaded = {}
    for b in blocks:
        if b["type"] == "text":
            t = (b.get("text") or "").strip()
            if t and not is_junk(t):
                parts.append(t)
        elif b["type"] == "img" and b.get("file"):
            fp = os.path.join(img_dir, b["file"])
            if b["file"] not in uploaded:
                uploaded[b["file"]] = upload_asset(fp, idx)
            p = uploaded[b["file"]]
            if p:
                parts.append(f"![图片]({p})")
    return "\n\n".join(parts)


def create_doc(notebook, title, markdown):
    r = post_json("/api/filetree/createDocWithMd", {
        "notebook": notebook, "path": "/", "markdown": markdown, "title": title,
    })
    doc_id = r.get("data", "")
    if not doc_id:
        raise RuntimeError(f"createDocWithMd failed: {r}")
    # 标题修复: 两步缺一不可
    post_json("/api/filetree/renameDoc", {"notebook": notebook, "path": f"/{doc_id}.sy", "title": title})
    post_json("/api/attr/setBlockAttrs", {
        "id": doc_id, "attrs": {"custom-sy-title-empty": "false", "title": title},
    })
    return doc_id


def rebuild_doc(doc_id, body, src_url, notebook=None, title=None):
    """重建已有文档内容，成功返回有效 doc_id（可能变化）。

    踩坑记录（2026-08-16，SiYuan 3.7.3）：
    - **deleteBlock 参数名是 `id`，不是 `blockID`**：传错参数名返回 code=0 但静默空操作
      （data=null；正确删除时 data 含 delete 操作记录）——曾因此导致 rebuild 残留旧块+追加新块=内容重复
    - SQL 查子块随机漏行（索引 bug）→ 查子块用 /api/block/getChildBlocks（实时、不走索引）
    - 防御：删除后循环校验子块为空；仍失败则 removeDoc 整篇删除 + create 重建（doc_id 会变，需回写）
    """
    src_md = f"> 来源：[知乎]({src_url})\n\n{body}"
    deleted = False
    for _ in range(5):
        r = post_json("/api/block/getChildBlocks", {"id": doc_id})
        children = r.get("data") or []
        if not children:
            deleted = True
            break
        for ch in children:
            post_json("/api/block/deleteBlock", {"id": ch["id"]})  # 参数名必须是 id（官方文档）；传 blockID 会静默空操作
        time.sleep(0.3)
    if deleted:
        resp = post_json("/api/block/insertBlock", {
            "dataType": "markdown", "data": src_md, "parentID": doc_id, "previousID": "",
        })
        if resp.get("code") != 0:
            raise RuntimeError(f"insertBlock failed: {resp}")
        return doc_id
    # 兜底：deleteBlock 静默失败 → 整篇删除重建
    if not (notebook and title):
        raise RuntimeError(f"删子块失败（deleteBlock 静默失败）且缺少 notebook/title 无法兜底: {doc_id}")
    doc = (post_json("/api/block/getBlockInfo", {"id": doc_id}).get("data") or {})
    path = doc.get("path")
    if not path:
        raise RuntimeError(f"无法获取文档路径: {doc_id}")
    post_json("/api/filetree/removeDoc", {"notebook": notebook, "path": path})
    return create_doc(notebook, title, src_md)


# ---------- 子命令 ----------

def load_articles(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_articles(path, articles):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)


def iter_article_blocks(articles, extract_dir):
    for art in articles:
        bf = os.path.join(extract_dir, f"{art['idx']}_blocks.json")
        if not os.path.exists(bf):
            print(f"[{art['idx']}] SKIP: 无抓取文件")
            continue
        with open(bf, encoding="utf-8") as f:
            data = json.load(f)
        yield art, data


def cmd_create(args):
    articles = load_articles(args.articles)
    for art, data in iter_article_blocks(articles, args.extract_dir):
        title = art.get("title") or art["url"]
        try:
            body = build_body(art["idx"], data["blocks"], os.path.join(args.extract_dir, "imgs"))
            if not body.strip():
                print(f"[{art['idx']}] SKIP: 生成为空 (blocks={len(data['blocks'])})")
                continue
            md = f"> 来源：[知乎]({art['url']})\n\n{body}"
            doc_id = create_doc(args.notebook, title, md)
            art["docId"] = doc_id
            time.sleep(0.6)
            new_md = export_md(doc_id)
            print(f"[{art['idx']}] OK doc={doc_id} {len(new_md)} 字符, 图片 {new_md.count('![')} 张")
            print(f"       链接: {API}/stage/build/desktop/?id={doc_id}")
        except Exception as e:
            print(f"[{art['idx']}] FAIL: {e}")
    save_articles(args.articles, articles)  # 回写 docId
    cmd_fix_assets(argparse.Namespace(notebook=args.notebook, articles=args.articles,
                                      extract_dir=args.extract_dir))  # 顺手归位资产


def cmd_rebuild(args):
    articles = load_articles(args.articles)
    for art, data in iter_article_blocks(articles, args.extract_dir):
        doc_id = art.get("docId")
        if not doc_id:
            print(f"[{art['idx']}] SKIP: articles.json 中无 docId")
            continue
        old_len = len(export_md(doc_id))
        try:
            body = build_body(art["idx"], data["blocks"], os.path.join(args.extract_dir, "imgs"))
            if not body.strip():
                print(f"[{art['idx']}] SKIP: 生成为空 (blocks={len(data['blocks'])})")
                continue
            new_doc_id = rebuild_doc(doc_id, body, art["url"], notebook=args.notebook, title=art["title"])
            if new_doc_id != doc_id:
                art["docId"] = new_doc_id  # 兜底重建后 doc_id 已变，回写
                doc_id = new_doc_id
            time.sleep(0.6)
            new_md = export_md(doc_id)
            print(f"[{art['idx']}] OK {old_len} -> {len(new_md)} 字符, 图片 {new_md.count('![')} 张")
        except Exception as e:
            print(f"[{art['idx']}] FAIL: {e}")
    save_articles(args.articles, articles)  # 回写可能变化的 docId
    cmd_fix_assets(argparse.Namespace(notebook=args.notebook, articles=args.articles,
                                      extract_dir=args.extract_dir))


def cmd_fix_assets(args):
    """把全局 /data/assets 中误存的图片复制到笔记本 assets 目录，使相对链接生效"""
    articles = load_articles(args.articles)
    doc_ids = [a["docId"] for a in articles if a.get("docId")]
    if not doc_ids:
        print("fix-assets: 无 docId，跳过")
        return
    asset_paths = set()
    for did in doc_ids:
        md = export_md(did)
        asset_paths.update(re.findall(r"!\[.*?\]\((assets/[^)]+)\)", md))
    ok = 0
    for p in sorted(asset_paths):
        name = os.path.basename(p)
        nb_path = f"/data/{args.notebook}/assets/{name}"
        try:
            get_file(nb_path)
            ok += 1  # 已存在
            continue
        except Exception:
            pass
        try:
            data = get_file(f"/data/assets/{name}")
            mime = "image/png" if name.endswith(".png") else "image/jpeg"
            r = put_file(nb_path, data, mime)
            back = get_file(nb_path)
            status = "OK" if r.get("code") == 0 and back == data else "FAIL"
            if status == "OK":
                ok += 1
            print(f"fix-assets {status} {name} {len(data)} bytes")
        except Exception as e:
            print(f"fix-assets FAIL {name}: {e}")
    print(f"fix-assets: {ok}/{len(asset_paths)} 就位")


def cmd_verify(args):
    articles = load_articles(args.articles)
    norm = lambda s: "".join(s.split())
    for art, data in iter_article_blocks(articles, args.extract_dir):
        if not art.get("docId"):
            continue
        md_norm = norm(export_md(art["docId"]))
        texts = [b["text"] for b in data["blocks"] if b["type"] == "text" and b.get("text")]
        hit = 0
        miss = []
        for t in texts:
            key = norm(t)[:15]
            if key and key in md_norm:
                hit += 1
            else:
                miss.append(t[:25])
        imgs = [b for b in data["blocks"] if b["type"] == "img"]
        print(f"[{art['idx']}] 文本块 {len(texts)}，命中 {hit} ({hit * 100 // max(len(texts), 1)}%)，图 {len(imgs)} 张"
              + (f" | 未命中例: {miss[:2]}" if miss and hit < len(texts) else ""))


def main():
    ap = argparse.ArgumentParser(description="知乎 -> 思源 写入流水线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create")
    p.add_argument("--notebook", required=True)
    p.add_argument("--articles", required=True)
    p.add_argument("--extract-dir", default="zhihu_extract")

    p = sub.add_parser("rebuild")
    p.add_argument("--notebook", required=True)
    p.add_argument("--articles", required=True)
    p.add_argument("--extract-dir", default="zhihu_extract")

    p = sub.add_parser("fix-assets")
    p.add_argument("--notebook", required=True)
    p.add_argument("--articles", required=True)
    p.add_argument("--extract-dir", default="zhihu_extract")

    p = sub.add_parser("verify")
    p.add_argument("--articles", required=True)
    p.add_argument("--extract-dir", default="zhihu_extract")

    args = ap.parse_args()
    {"create": cmd_create, "rebuild": cmd_rebuild,
     "fix-assets": cmd_fix_assets, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    main()
