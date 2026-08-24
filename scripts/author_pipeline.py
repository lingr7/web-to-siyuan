#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知乎答主合集写入思源：主文档（答主档案）+ 子文档（每篇回答）

用法:
  python author_pipeline.py create --notebook <nb_id> --articles <articles.json> --extract-dir <dir> \
      --author "罗心澄" --author-token <token> --author-about "<简介>"
  python author_pipeline.py verify --articles <articles.json> --extract-dir <dir>

复用 web-to-siyuan skill 的踩坑经验：
- createDocWithMd 标题不生效 → renameDoc + setBlockAttrs 两步
- asset/upload 落全局 /data/assets/ → fix-assets 复制到笔记本 assets
- 子文档: createDocWithMd 的 path 参数用 "/{父文档id}.sy/" 前缀
"""
import argparse
import json
import mimetypes
import os
import re
import sys
import time
import urllib.request
import uuid

API = "http://127.0.0.1:6806"

JUNK_PATTERNS = [
    re.compile(r"^\d+\s*人赞同了该回答$"),
    re.compile(r"^赞同\s*\d+$"),
    re.compile(r"^发布于\s*\d{4}"),
    re.compile(r"^编辑于\s*\d{4}"),
    re.compile(r"^收录于"),
    re.compile(r"^被\s*\d+\s*人收藏"),
    re.compile(r"^分享$"),
    re.compile(r"^赞同$"),
    re.compile(r"^收藏$"),
    re.compile(r"^喜欢$"),
    re.compile(r"^评论\s*\d+$"),
    re.compile(r"^更多$"),
    re.compile(r"^阅读全文$"),
    re.compile(r"^收起$"),
    re.compile(r"^想读$"),
    re.compile(r"^推荐阅读"),
    re.compile(r"^·\s*$"),
    re.compile(r"^\s*$"),
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
    """上传图片资产（落全局 /data/assets/，之后 fix-assets 归位）"""
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


def create_doc(notebook, title, markdown, parent_path="/"):
    """创建文档。parent_path 传 "/" 或 "/{父文档id}.sy/" 实现子文档"""
    r = post_json("/api/filetree/createDocWithMd", {
        "notebook": notebook, "path": parent_path, "markdown": markdown, "title": title,
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


# ---------- 子命令 ----------

def load_articles(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_gen_articles(args):
    """从 crawl_state.json 生成 articles.json（按 URL 排序，idx 稳定，仅 verified 的 answer）"""
    with open(args.state, encoding="utf-8") as f:
        state = json.load(f)
    items = [(url, info) for url, info in state.get("discovered", {}).items()
             if info.get("type") == "answer"]
    # verified 的优先收录；未 verified 的也保留（verified: false），后续 crawler 补验
    items.sort(key=lambda kv: kv[0])
    # 保留已有 docId（避免重复 create 产生重复文档）
    old = {}
    if os.path.exists(args.articles):
        try:
            with open(args.articles, encoding="utf-8") as f:
                old = {a["url"]: a.get("docId") or "" for a in json.load(f) if a.get("url")}
        except Exception:
            pass
    articles = []
    for i, (url, info) in enumerate(items):
        articles.append({
            "idx": str(i + 1).zfill(3),
            "type": "answer",
            "url": url,
            "title": (info.get("title") or f"回答 {info.get('answerId', '')}").strip(),
            "docId": old.get(url, ""),
            "verified": bool(info.get("verified")),
            "meta": {
                "answerId": str(info.get("answerId") or ""),
                "questionId": str(info.get("questionId") or ""),
                "foundFrom": info.get("foundFrom") or "",
            },
        })
    with open(args.articles, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
    n_verified = sum(1 for a in articles if a["verified"])
    print(f"生成 {len(articles)} 条（verified={n_verified}）→ {args.articles}")


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
    author_name = args.author or "知乎用户"
    author_token = args.author_token or ""
    about = args.author_about or ""

    # 1. 创建主文档（答主档案）
    profile_md = []
    profile_md.append(f"# {author_name}（知乎答主合集）")
    profile_md.append("")
    profile_md.append(f"> 知乎主页：[{author_name}](https://www.zhihu.com/people/{author_token})")
    if about:
        profile_md.append("")
        profile_md.append(about)
    profile_md.append("")
    profile_md.append(f"共收录 **{len(articles)}** 篇回答（递归发现，可能未穷尽全部 1283 篇，因答主开启隐私保护无法直接列出全部）")
    profile_md.append("")
    profile_md.append("## 目录")
    profile_md.append("")
    for art in articles:
        title = (art.get("title") or "未命名回答").strip()
        # 目录块用锚点不方便，直接列问题标题 + 子文档引用由思源文档树呈现
        profile_md.append(f"- {title}")
    profile_md.append("")

    parent_doc_id = None
    profile_title = f"{author_name} - 知乎回答合集"
    # 先检查是否已存在主文档（幂等）
    try:
        r = post_json("/api/query/sql", {
            "stmt": f"SELECT id FROM blocks WHERE type='d' AND content='{profile_title}' AND box='{args.notebook}' LIMIT 1"
        })
        rows = r.get("data") or []
        if rows:
            parent_doc_id = rows[0]["id"]
            print(f"[主文档] 已存在: {parent_doc_id}")
        else:
            parent_doc_id = create_doc(args.notebook, profile_title, "\n".join(profile_md))
            print(f"[主文档] 创建 OK: {parent_doc_id}")
            print(f"         链接: {API}/stage/build/desktop/?id={parent_doc_id}")
    except Exception as e:
        print(f"[主文档] 查询失败，直接创建: {e}")
        parent_doc_id = create_doc(args.notebook, profile_title, "\n".join(profile_md))
        print(f"[主文档] 创建 OK: {parent_doc_id}")

    parent_path = f"/{parent_doc_id}.sy/"

    # 2. 创建子文档（每篇回答）
    ok_count = 0
    for art, data in iter_article_blocks(articles, args.extract_dir):
        title = (art.get("title") or art["url"]).strip()
        if len(title) > 60:
            title = title[:60] + "…"
        try:
            body = build_body(art["idx"], data["blocks"], os.path.join(args.extract_dir, "imgs"))
            if not body.strip():
                print(f"[{art['idx']}] SKIP: 生成为空 (blocks={len(data['blocks'])})")
                continue
            md = f"> 来源：[知乎]({art['url']})\n\n{body}"
            doc_id = create_doc(args.notebook, title, md, parent_path=parent_path)
            art["docId"] = doc_id
            ok_count += 1
            time.sleep(0.5)
            new_md = export_md(doc_id)
            print(f"[{art['idx']}] OK doc={doc_id} {len(new_md)} 字符, 图片 {new_md.count('![')} 张")
            print(f"       链接: {API}/stage/build/desktop/?id={doc_id}")
        except Exception as e:
            print(f"[{art['idx']}] FAIL: {e}")

    # 3. 回写 docId + 主文档 id
    with open(os.path.join(os.path.dirname(args.articles), "author_main_doc.json"), "w", encoding="utf-8") as f:
        json.dump({"mainDocId": parent_doc_id, "notebook": args.notebook}, f, ensure_ascii=False, indent=2)
    save_articles(args.articles, articles)

    # 4. 资产归位
    print(f"\n成功创建 {ok_count}/{len(articles)} 个子文档")
    fix_assets(args.notebook, articles)


def fix_assets(notebook, articles):
    doc_ids = [a["docId"] for a in articles if a.get("docId")]
    if not doc_ids:
        print("fix-assets: 无 docId，跳过")
        return
    asset_paths = set()
    for did in doc_ids:
        try:
            md = export_md(did)
            asset_paths.update(re.findall(r"!\[.*?\]\((assets/[^)]+)\)", md))
        except Exception as e:
            print(f"fix-assets 导出失败 {did}: {e}")
    ok = 0
    for p in sorted(asset_paths):
        name = os.path.basename(p)
        nb_path = f"/data/{notebook}/assets/{name}"
        try:
            get_file(nb_path)
            ok += 1
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
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create")
    p.add_argument("--notebook", required=True)
    p.add_argument("--articles", required=True)
    p.add_argument("--extract-dir", required=True)
    p.add_argument("--author", default="")
    p.add_argument("--author-token", default="")
    p.add_argument("--author-about", default="")

    g = sub.add_parser("gen-articles")
    g.add_argument("--state", required=True)
    g.add_argument("--articles", required=True)

    v = sub.add_parser("verify")
    v.add_argument("--articles", required=True)
    v.add_argument("--extract-dir", required=True)

    args = parser.parse_args()
    if args.cmd == "create":
        cmd_create(args)
    elif args.cmd == "gen-articles":
        cmd_gen_articles(args)
    elif args.cmd == "verify":
        cmd_verify(args)


if __name__ == "__main__":
    main()
