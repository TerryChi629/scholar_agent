"""文献入库: PDF -> 解析 -> 语义分块 -> embedding -> 向量库。

分块策略 (M1):
- 用 PyMuPDF 的 dict 模式拿到每行文本 + 字号 + 页码。
- 以"字号明显大于正文"识别章节标题, 据此把全文切成 section (Abstract/Introduction/...)。
- section 内再按字符预算二次切分, 控制单 chunk 长度; 切分不跨 section。
- 每个 chunk 保留 section 名 + 起始页码 -> 供 evidence_spans 回溯。

元数据抽取:
- title: 首页最大字号的连续文本行。
- year: 优先 PDF creationDate, 回退正文中的 arXiv 编号 / 4 位年份。
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rag.store import Chunk, get_store


def _paper_id(path: Path) -> str:
    return hashlib.md5(str(path).encode()).hexdigest()[:12]


@dataclass
class _Line:
    page: int
    text: str
    size: float


def _extract_lines(doc) -> list[_Line]:
    """逐页用 dict 模式抽取每行文本与最大字号。"""
    lines: list[_Line] = []
    for pno, page in enumerate(doc):
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s["text"] for s in spans).strip()
                if not text:
                    continue
                size = round(max(s["size"] for s in spans), 1)
                lines.append(_Line(page=pno, text=text, size=size))
    return lines


def _body_size(lines: list[_Line]) -> float:
    """正文字号 = 承载字符最多的字号 (出现频率按文本长度加权)。"""
    counter: Counter[float] = Counter()
    for ln in lines:
        counter[ln.size] += len(ln.text)
    return counter.most_common(1)[0][0] if counter else 10.0


# 常见章节标题关键词 (大小写不敏感), 用于在字号判定外补充识别。
_SECTION_KEYWORDS = re.compile(
    r"^(abstract|introduction|related work|background|preliminaries|"
    r"method(s|ology)?|approach|model|framework|experiment(s)?|evaluation|"
    r"result(s)?|analysis|discussion|conclusion(s)?|references|appendix|acknowledg)",
    re.IGNORECASE,
)


# 噪声行 (作者署名 / 邮箱 / arXiv 水印 / 脚注), 不应作为标题或章节。
_NOISE_LINE = re.compile(r"@|[∗*†‡]\s*$|arXiv:\d", re.IGNORECASE)


def _is_heading(text: str, size: float, body: float) -> bool:
    """判定一行是否为章节标题: 字号显著大于正文且文本短; 或命中章节关键词。"""
    if len(text) > 60 or not re.search(r"[A-Za-z]", text):
        return False
    if _NOISE_LINE.search(text):
        return False
    big = size >= body * 1.15
    # 形如 "1 Introduction" / "3.1 Method" / "INTRODUCTION"
    numbered = bool(re.match(r"^\d{1,2}(\.\d{1,2})*\.?\s+\S", text))
    keyword = bool(_SECTION_KEYWORDS.match(text.lstrip("0123456789. ")))
    return big or (numbered and keyword) or (keyword and len(text) <= 35)


def _split_sections(lines: list[_Line], body: float) -> list[tuple[str, list[_Line]]]:
    """按标题行把文档切成 [(section_name, lines)]。标题前的内容归入 '_front'。"""
    sections: list[tuple[str, list[_Line]]] = []
    cur_name = "_front"
    cur_lines: list[_Line] = []
    for ln in lines:
        if _is_heading(ln.text, ln.size, body):
            if cur_lines:
                sections.append((cur_name, cur_lines))
            cur_name = ln.text
            cur_lines = []
        else:
            cur_lines.append(ln)
    if cur_lines:
        sections.append((cur_name, cur_lines))
    return sections


# 纯编号 / 孤立小标题残行 (如 "3.1"、"4.2.1"), 不应单独成句, 并入后文。
_NUM_ONLY_RE = re.compile(r"^\d{1,2}(\.\d{1,2})*\.?$")
# 句子结束符 (中英)。在其后切句。
_SENT_END_RE = re.compile(r"(?<=[.!?。！？])\s+|(?<=[。！？])")


def _is_meaningful(text: str) -> bool:
    """判定一个 chunk 是否含实质内容 (过滤纯编号 / 过短孤片)。

    section 切分偶尔会把孤立的章节编号 (如 "3.1") 留成独立 chunk, 对检索是噪声;
    要求去掉编号/标点后至少有若干个字母或 CJK 字, 才认为有检索价值。
    """
    t = (text or "").strip()
    if not t or _NUM_ONLY_RE.match(t):
        return False
    # 实质字符 = 字母 + CJK 字; 过少 (如仅编号、单词碎片) 视为无信息。
    meaningful = re.findall(r"[A-Za-z]|[\u4e00-\u9fff]", t)
    return len(meaningful) >= 10


def _join_lines(lines: list[_Line]) -> tuple[str, list[tuple[int, int]]]:
    """把一个 section 的行拼成连续文本, 复原 PDF 跨行断词。

    PDF 抽取常把单词在行末用连字符断开 (如 "de-\\nsigned"), 直接按行拼会留下
    "de- signed" 甚至硬切出 "igned ..."。这里:
    - 行末连字符 + 下一行: 去连字符直接拼 (de-signed -> designed);
    - 否则用空格拼 (正常的换行视作词间空格)。
    同时返回 [(char_offset, page)] 标记, 供按字符偏移回溯页码 (保住多页 section 的页码精度)。
    """
    out = ""
    marks: list[tuple[int, int]] = []
    for ln in lines:
        t = ln.text.strip()
        if not t:
            continue
        if not out:  # 首行: 直接作为开头
            marks.append((0, ln.page))
            out = t
        elif out.endswith("-"):  # 行末连字符: 断词, 去掉连字符直接接续
            out = out[:-1]
            marks.append((len(out), ln.page))
            out += t
        else:  # 普通换行: 视作词间空格
            out += " "
            marks.append((len(out), ln.page))
            out += t
    return out, marks


def _page_at(offset: int, marks: list[tuple[int, int]], default: int) -> int:
    """按字符偏移取该位置所在页 (取最后一个 offset <= 目标 的标记页)。"""
    page = default
    for off, pg in marks:
        if off <= offset:
            page = pg
        else:
            break
    return page


def _split_sentences(text: str) -> list[tuple[str, int]]:
    """按句末标点切句, 返回 [(sentence, start_offset)]。

    纯编号孤片 (如 "3.1") 并入下一句, 避免成为残片。start_offset 为该句在原文中的
    起始字符位置, 供回溯页码。
    """
    parts: list[tuple[str, int]] = []
    pos = 0
    for piece in _SENT_END_RE.split(text):
        start = text.find(piece, pos) if piece else pos
        if piece.strip():
            parts.append((piece.strip(), start if start >= 0 else pos))
        pos = (start if start >= 0 else pos) + len(piece)

    sents: list[tuple[str, int]] = []
    carry = ""
    carry_off = 0
    for s, off in parts:
        if _NUM_ONLY_RE.match(s):  # 纯编号: 暂存, 拼到下一句开头
            if not carry:
                carry_off = off
            carry = (carry + " " + s).strip()
            continue
        if carry:
            s = f"{carry} {s}"
            off = carry_off
            carry = ""
        sents.append((s, off))
    if carry:  # 末尾残留的编号: 并到最后一句
        if sents:
            sents[-1] = (f"{sents[-1][0]} {carry}", sents[-1][1])
        else:
            sents.append((carry, carry_off))
    return sents


def _chunk_section(
    section: str, lines: list[_Line], size: int, overlap: int
) -> list[tuple[str, int, str]]:
    """把一个 section 按句子边界切成 chunk (绝不切在句中)。

    先复原跨行断词拼成连续文本并断句, 再按字符预算逐句累积; 超预算即出一个 chunk,
    并用尾部整句 (而非字符) 作为下一个 chunk 的 overlap, 保证片段读起来是完整句子。
    返回 [(section, page, text)]; page 按 chunk 首句的字符偏移回溯真实页。不跨 section。
    """
    if not lines:
        return []
    text, marks = _join_lines(lines)
    sents = _split_sentences(text)
    if not sents:
        return []
    default_page = lines[0].page

    out: list[tuple[str, int, str]] = []
    buf: list[tuple[str, int]] = []
    buf_len = 0
    for s, off in sents:
        buf.append((s, off))
        buf_len += len(s) + 1
        if buf_len >= size:
            page = _page_at(buf[0][1], marks, default_page)
            out.append((section, page, " ".join(x[0] for x in buf).strip()))
            # 整句级 overlap: 从尾部回取若干句, 累计长度不超过 overlap。
            keep: list[tuple[str, int]] = []
            klen = 0
            for prev in reversed(buf):
                if klen + len(prev[0]) > overlap:
                    break
                keep.insert(0, prev)
                klen += len(prev[0]) + 1
            buf = keep
            buf_len = klen
    if buf and " ".join(x[0] for x in buf).strip():
        page = _page_at(buf[0][1], marks, default_page)
        out.append((section, page, " ".join(x[0] for x in buf).strip()))
    return out


def parse_pdf_text(path: Path) -> str:
    """用 PyMuPDF 抽取全文纯文本 (保留, 供调试 / 兼容旧调用)。"""
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    return "\n".join(page.get_text() for page in doc)


def _extract_title(lines: list[_Line]) -> str:
    """标题 = 首页最大字号的连续行 (拼接), 跳过 arXiv 水印等噪声。"""
    first_page = [ln for ln in lines if ln.page == 0 and not _NOISE_LINE.search(ln.text)]
    if not first_page:
        return ""
    max_size = max(ln.size for ln in first_page)
    title_lines = [ln.text for ln in first_page if ln.size == max_size]
    title = " ".join(title_lines).strip()
    return title[:300]


def _extract_year(doc, full_text: str) -> int:
    """年份: 优先 PDF creationDate (D:YYYY...), 回退正文 arXiv 编号或 4 位年份。"""
    raw = doc.metadata.get("creationDate") or ""
    m = re.search(r"D:(\d{4})", raw)
    if m:
        y = int(m.group(1))
        if 1990 <= y <= 2100:
            return y
    # arXiv:YYMM.xxxxx -> 20YY
    m = re.search(r"arXiv:(\d{2})\d{2}\.", full_text)
    if m:
        return 2000 + int(m.group(1))
    m = re.search(r"\b(19|20)\d{2}\b", full_text)
    return int(m.group(0)) if m else 0


def parse_pdf(path: Path) -> dict:
    """解析单篇 PDF, 返回 {title, year, chunks:[(section,page,text)]}。"""
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    lines = _extract_lines(doc)
    if not lines:  # OCR 回退占位: 当前直接返回空
        return {"title": path.stem, "year": 0, "chunks": []}

    body = _body_size(lines)
    full_text = "\n".join(ln.text for ln in lines)
    title = _extract_title(lines) or path.stem
    year = _extract_year(doc, full_text)

    chunks: list[tuple[str, int, str]] = []
    for section, sec_lines in _split_sections(lines, body):
        chunks.extend(_chunk_section(section, sec_lines, size=800, overlap=120))
    # 过滤无信息 chunk: 纯章节编号残片 (如 "3.1") 或实质字符过少的孤片。
    chunks = [c for c in chunks if _is_meaningful(c[2])]
    return {"title": title, "year": year, "chunks": chunks}


def ingest_dir(directory: str) -> dict:
    """扫描目录下所有 PDF, 增量入库 (跳过库内已存在的 paper_id), 返回统计。

    增量策略: 以 _paper_id(path) 为身份, 已在向量库存在的论文直接跳过 (不解析、
    不 embedding), 只处理新增。配合 store.add 的 upsert, 重复 ingest 既不报错也不浪费。
    """
    store = get_store()
    existing = set(store.list_papers().keys())
    root = Path(directory).expanduser()
    pdfs = list(root.glob("**/*.pdf"))
    total_chunks = 0
    added = 0
    skipped = 0
    papers: list[dict] = []
    for pdf in pdfs:
        pid = _paper_id(pdf)
        if pid in existing:
            skipped += 1
            papers.append({"paper_id": pid, "title": pdf.stem, "status": "skipped"})
            continue
        parsed = parse_pdf(pdf)
        chunks = [
            Chunk(
                chunk_id=f"{pid}_{i}",
                paper_id=pid,
                text=text,
                metadata={
                    "title": parsed["title"],
                    "year": parsed["year"],
                    "source": str(pdf),
                    "section": section,
                    "page": page,
                    "chunk_index": i,
                },
            )
            for i, (section, page, text) in enumerate(parsed["chunks"])
        ]
        store.add(chunks)
        # 增量更新持久化 BM25 索引 (与向量库同步, 避免下次查询全量重建)。
        try:
            from rag.bm25_index import add_chunks as bm25_add
            bm25_add(chunks)
        except Exception:  # noqa: BLE001  索引更新失败不应阻断入库 (查询侧会回退重建)
            pass
        existing.add(pid)  # 防同次目录内重复路径再次处理
        total_chunks += len(chunks)
        added += 1
        papers.append({"paper_id": pid, "title": parsed["title"],
                       "year": parsed["year"], "chunks": len(chunks), "status": "added"})
    return {"papers": len(pdfs), "added": added, "skipped": skipped,
            "chunks": total_chunks, "detail": papers}
