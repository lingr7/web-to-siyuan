# 思源笔记 API 坑位详解

以下均为实测验证（思源本地 API，端口 6806）。逐条踩过，修复方案已固化在 `scripts/siyuan_pipeline.py` 中。

## 1. 端口固定 6806

52926 等旧端口不可用。所有 API 基址为 `http://127.0.0.1:6806`。

## 2. `createDocWithMd` 标题不生效

用 markdown 创建文档后，文档会显示"未命名文档"，`title` 参数不生效。

**修复（两步缺一不可）**：

```python
# ① renameDoc
post_json("/api/filetree/renameDoc", {"notebook": notebook, "path": f"/{doc_id}.sy", "title": title})
# ② setBlockAttrs
post_json("/api/attr/setBlockAttrs", {
    "id": doc_id,
    "attrs": {"custom-sy-title-empty": "false", "title": title},
})
```

## 3. `updateBlock` 对文档根块静默失败

对文档根块（`type=d`）调用 `updateBlock` 返回 `code=0` 表示成功，但内容**不会持久化**（重开文档即还原）。

**修复**：改用「SQL 查子块 id → 逐个 `deleteBlock` → `insertBlock(parentID=doc_id)`」重建，已验证可持久化。见 `siyuan_pipeline.py` 的 `rebuild_doc()`。

## 4. `/api/asset/upload` 落错目录

上传的文件存到**全局** `/data/assets/`，而文档内 `assets/xxx` 相对链接按**笔记本**目录解析 → 图片在文档中 404。

**修复**：用 `/api/file/getFile` 从全局目录读出，再 `/api/file/putFile` 写到 `/data/<notebook>/assets/`。见 `fix-assets` 子命令，且带字节级回读校验。

## 5. 上传文件字段名是 `file[]`

`/api/asset/upload` 的 multipart 文件字段名是 `file[]`，不是 `files[]` 或 `file`。

## 6. SQL 查询端点细节

- 端点：`/api/query/sql`，POST
- 参数名：`stmt`（不是 `sql`）
- 图片在文档中是内联 markdown 语法，SQL 里 `type='img'` 查不到独立图片块

## 7. JSON 转义

标题/正文含引号（中文引号也会）时，不要用 shell 拼 JSON，用 Python 脚本构造请求体（`json.dumps` 自动转义）。
